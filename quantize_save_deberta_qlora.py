# coding=utf-8
# Copyright 2023-present the HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import os
import functools
from scipy.stats import norm
from functools import partial

import torch
import torch.nn as nn
from transformers import (
    AutoModelForCausalLM,
    AutoModelForSeq2SeqLM,
    AutoModelForSequenceClassification,
    AutoTokenizer,
)

from peft import LoraConfig, TaskType, get_peft_model
from peft.tuners import lora
import bitsandbytes as bnb
from utils import NFQuantizer


class Shell(nn.Module):
    def __init__(self, weight, bias=None):
        super().__init__()
        self.weight = nn.Parameter(weight, requires_grad=False)
        if bias is not None:
            self.bias = nn.Parameter(bias, requires_grad=False)


def unwrap_model(model, sub_module_name=".base_layer"):
    sub_module_name_list = [k.split(sub_module_name)[0] for k in model.state_dict().keys() if sub_module_name in k]
    sub_module_name_set = set(sub_module_name_list)
    for name in sub_module_name_set:
        # get the parent of the submodule
        name_parent = ".".join(name.split(".")[:-1])
        name_child = name.split(".")[-1]
        sub_module = model.get_submodule(name_parent)
        print(sub_module)

        # replace with shell
        child = getattr(sub_module, name_child)
        weight = getattr(child.base_layer, "weight", None)
        bias = getattr(child.base_layer, "bias", None)
        shell = Shell(weight, bias)

        setattr(sub_module, name_child, shell)

    print("You have unwrapped the model. Use it on your own risk.")


def print_model(model, name):
    print("=" * 10 + name + "=" * 10)
    print(model)
    for name, param in model.named_parameters():
        if torch.is_tensor(param):
            if param.dtype in [torch.float32, torch.float16]:
                print(
                    name,
                    param.shape,
                    param.device,
                    param.dtype,
                    param.requires_grad,
                    param.mean().item(),
                    param.max().item(),
                )
            else:
                print(name, param.shape, param.device, param.dtype, param.requires_grad)


def arg_parse():
    parser = argparse.ArgumentParser(description="Quantize a model with LoftQ.")
    parser.add_argument(
        "--model_name_or_path",
        type=str,
        default=None,
        required=True,
        help="The name or path of the fp32/16 model.",
    )
    parser.add_argument(
        "--token",
        type=str,
        default=None,
        help="The access token to download model from HuggingFace Hub.",
    )
    parser.add_argument(
        "--bits",
        type=int,
        default=4,
        help="The quantized bits",
    )
    parser.add_argument(
        "--iter",
        type=int,
        default=1,
        help="The alternating steps in LoftQ",
    )
    parser.add_argument(
        "--rank",
        type=int,
        default=16,
        help="The rank of the LoRA adapter",
    )
    parser.add_argument(
        "--save_dir",
        type=str,
        default="./model_zoo/loftq/",
        help="The rank of the LoRA adapter",
    )
    parser.add_argument(
        "--scaling",
        action='store_true',
        help="Take scaling into account.",
    )
    parser.add_argument(
        "--device",
        type=str,
        default="cuda",
    )
    args = parser.parse_args()
    return args


def quantize_and_save():
    args = arg_parse()

    # Download weights and configure LoRA
    tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path, token=args.token, trust_remote_code=True)
    if any(name in args.model_name_or_path.lower() for name in ["llama", "mistral", "falcon"]):
        model = AutoModelForCausalLM.from_pretrained(
            args.model_name_or_path,
            torch_dtype=torch.bfloat16,
            token=args.token,
            trust_remote_code=True,
            device_map=args.device,
        )
        task_type = TaskType.CAUSAL_LM
        target_modules = ["q_proj", "k_proj", "v_proj", "o_proj", "up_proj", "down_proj", "gate_proj"]

    elif any(name in args.model_name_or_path.lower() for name in ["bart", "t5"]):
        model = AutoModelForSeq2SeqLM.from_pretrained(
            args.model_name_or_path,
            token=args.token,
            device_map=args.device
        )
        task_type = TaskType.SEQ_2_SEQ_LM
        target_modules = ["q_proj", "k_proj", "v_proj", "fc1", "fc2", "out_proj"]

    elif any(name in args.model_name_or_path.lower() for name in ["deberta", "roberta", "bert"]):
        model = AutoModelForSequenceClassification.from_pretrained(
            args.model_name_or_path,
            token=args.token,
            device_map=args.device
        )
        task_type = TaskType.SEQ_CLS
        target_modules = ["query_proj", "key_proj", "value_proj", "attention.output.dense"]  # embeddings not supported by peft
    else:
        raise NotImplementedError("Other models not supported yet.")


    print("Original Model:")
    print(model)

    quantizer = NFQuantizer(
        num_bits=args.bits,
        device="cuda",
        method="normal",
        block_size=64
    )
    def quantize_linear_hook(m, x, y, name):
        print(f"========={name}==========")
        dtype = m.weight.dtype
        weight = m.weight.clone()
        weight = weight.to(device=args.device, dtype=torch.float32)
        """
        qweight = bnb.nn.Params4bit(
            weight.to("cpu"),
            requires_grad=False,
            compress_statistics=False,
            quant_type="nf4"
        ).to(args.device)
        dequantized_weight = bnb.functional.dequantize_4bit(
            qweight.data,
            qweight.quant_state,
            quant_type="nf4"
        )
        """
        q_weight, max_abs, shape = quantizer.quantize_block(weight)
        deq_weight = quantizer.dequantize_block(q_weight, max_abs, shape)
        m.weight.data = deq_weight.to(device=args.device, dtype=dtype)
        print(f"Error: {torch.norm(weight - deq_weight)}")


    with torch.no_grad():
        hooks = []
        for name, m in model.named_modules():
            if isinstance(m, torch.nn.Linear) and not ("pooler" in name) and not ("classifier" in name):
                hooks.append(
                    m.register_forward_hook(
                        functools.partial(quantize_linear_hook, name=name))
                )

        input_ids = torch.arange(1, 10).unsqueeze(0).to(args.device)
        model(input_ids)

        for hook in hooks:
            hook.remove()

    # LoRA Config
    lora_config = LoraConfig(
        task_type=task_type,
        inference_mode=True,
        r=args.rank,
        lora_alpha=16 if task_type is TaskType.CAUSAL_LM else args.rank,
        lora_dropout=0.1,
        target_modules=target_modules,
    )

    # Obtain LoftQ model
    lora_model = get_peft_model(model, lora_config)
    print(lora_model)

    # Save
    base_model = lora_model.get_base_model()

    # Save LoftQ model
    model_name = args.model_name_or_path.split("/")[-1] + f"-{args.bits}bit" + f"-{args.rank}rank"
    base_model_dir = os.path.join(args.save_dir, model_name)
    lora_model_dir = os.path.join(args.save_dir, model_name, "loftq_init")

    # save lora adapters first
    lora_model.base_model.peft_config[
        "default"
    ].base_model_name_or_path = base_model_dir  # This can be a local path or Hub model id
    lora_model.base_model.peft_config["default"].init_lora_weights = True  # Don't apply LoftQ when loading again

    lora_model.save_pretrained(lora_model_dir)
    print_model(lora_model, "lora_model")

    # remove lora adapters and save the backbone
    unwrap_model(base_model)
    base_model.save_pretrained(base_model_dir)
    tokenizer.save_pretrained(base_model_dir)

    print_model(base_model, "base_model")

    return base_model_dir, lora_model_dir


if __name__ == "__main__":
    base_dir, lora_dir = quantize_and_save()

# example command:
# python quantize_save_load.py \
# --model_name_or_path meta-llama/Llama-2-7b-hf \
# --token XXX \
# --bits 4 --iter 5 --rank 16 \
# --save_dir ./model_zoo/loftq/
