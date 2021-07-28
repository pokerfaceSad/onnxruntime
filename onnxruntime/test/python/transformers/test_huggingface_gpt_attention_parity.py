# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation.  All rights reserved.
# Licensed under the MIT License.  See License.txt in the project root for
# license information.
# --------------------------------------------------------------------------
# Copyright 2020 The HuggingFace Inc. team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
# --------------------------------------------------------------------------

import unittest
import pytest
import torch
from torch import nn
import random
from onnx import helper
import onnx
from packaging import version
import numpy
import os
from onnxruntime.transformers.gpt2_helper import Gpt2Helper
from transformers.modeling_utils import Conv1D

DEBUG_OUTPUTS = ["qk", "norm_qk", "softmax", "attn_weights"]
#DEBUG_OUTPUTS = ["softmax"]
#DEBUG_OUTPUTS = ["qk"]

class MyGPT2Attention(nn.Module):
    """
    This module is modifed from Gpt2Attention of huggingface transformers v4.9.1.
    Code related to crosss attention, c_proj, attn_dropout and head_mask etc are removed.
    """
    def __init__(self, max_position_embeddings=1024, hidden_size=768, num_attention_heads=12, use_cache=True, debug=False, fix_onnx_export=True):
        super().__init__()
        max_positions = max_position_embeddings
        self.register_buffer(
            "bias",
            torch.tril(torch.ones((max_positions, max_positions), dtype=torch.uint8)).view(
                1, 1, max_positions, max_positions
            ),
        )
        self.register_buffer("masked_bias", torch.tensor(-1e4))
        self.embed_dim = hidden_size
        self.num_heads = num_attention_heads
        self.head_dim = self.embed_dim // self.num_heads
        self.split_size = self.embed_dim
        assert self.head_dim * self.num_heads == self.embed_dim

        self.c_attn = Conv1D(3 * self.embed_dim, self.embed_dim)
        # use random bias instead of zeros for parity test
        self.c_attn.bias = nn.Parameter(torch.normal(0.0, 0.1, (3 * self.embed_dim,)))

        self.use_cache = use_cache
        self.debug = debug
        self.fix_onnx_export = fix_onnx_export

    def _attn(self, query, key, value, attention_mask=None):
        qk = torch.matmul(query, key.transpose(-1, -2))

        # Torch has special handling for Div and Mul by a scalar:
        #   https://github.com/pytorch/pytorch/blob/5536cda19a5def9e0553b318f04d297d602ac956/aten/src/ATen/native/cuda/BinaryMulDivKernel.cu#L52-L60
        #   https://github.com/pytorch/pytorch/blob/5536cda19a5def9e0553b318f04d297d602ac956/aten/src/ATen/native/cuda/BinaryMulDivKernel.cu#L185-L194
        # Modify the code to use same processing in onnx export so as to get parity result
        if self.fix_onnx_export and torch.onnx.is_in_onnx_export():
            if qk.dtype == torch.float16:
                norm_qk = qk.to(torch.float32) * (1.0 / (float(value.size(-1)) ** 0.5))
                norm_qk = norm_qk.to(torch.float16)
            else:
                norm_qk = qk * (1.0 / (float(value.size(-1)) ** 0.5))
        else:
            norm_qk = qk / (float(value.size(-1)) ** 0.5)

        query_length, key_length = query.size(-2), key.size(-2)
        causal_mask = self.bias[:, :, key_length - query_length : key_length, :key_length].bool()
        attn_weights = torch.where(causal_mask, norm_qk, self.masked_bias.to(norm_qk.dtype))
           
        if attention_mask is not None:
            attn_weights = attn_weights + attention_mask

        softmax = nn.Softmax(dim=-1)(attn_weights)

        attn_output = torch.matmul(softmax, value)

        if self.debug:
            return attn_output, qk, norm_qk, softmax, attn_weights
        else:
            return attn_output

    def _split_heads(self, tensor, num_heads, attn_head_size):
        new_shape = tensor.size()[:-1] + (num_heads, attn_head_size)
        tensor = tensor.view(*new_shape)
        return tensor.permute(0, 2, 1, 3)  # (batch, head, seq_length, head_features)

    def _merge_heads(self, tensor, num_heads, attn_head_size):
        tensor = tensor.permute(0, 2, 1, 3).contiguous()
        new_shape = tensor.size()[:-2] + (num_heads * attn_head_size,)
        return tensor.view(new_shape)

    @staticmethod
    def concat_key_value(key, value):
        return torch.cat((key.unsqueeze(0), value.unsqueeze(0)), dim=0)

    @staticmethod
    def process_mask(attention_mask, dtype):
        # Create a 4D attention mask with shape [batch_size, 1, 1, to_seq_length] from a 2D tensor mask.
        attention_mask = attention_mask[:, None, None, :]
        attention_mask = attention_mask.to(dtype=dtype)  # fp16 compatibility
        attention_mask = (1.0 - attention_mask) * -10000.0
        return attention_mask

    def forward(
        self,
        hidden_states,
        attention_mask=None,
        layer_past=None
    ):
        query, key, value = self.c_attn(hidden_states).split(self.split_size, dim=2)

        query = self._split_heads(query, self.num_heads, self.head_dim)
        key = self._split_heads(key, self.num_heads, self.head_dim)
        value = self._split_heads(value, self.num_heads, self.head_dim)

        if layer_past is not None:
            past_key, past_value = layer_past
            key = torch.cat((past_key, key), dim=-2)
            value = torch.cat((past_value, value), dim=-2)

        if self.use_cache is True:
            # Instead of present = (key, value), here we merge them into one tensor to be compatible with Attention operator.
            present = MyGPT2Attention.concat_key_value(key, value)
        else:
            present = None

        mask = MyGPT2Attention.process_mask(attention_mask, dtype=query.dtype) # mask processing is moved to here.

        if self.debug:
            attn_output, qk, norm_qk, softmax, attn_weights = self._attn(query, key, value, mask)
        else:
            attn_output = self._attn(query, key, value, mask)

        attn_output = self._merge_heads(attn_output, self.num_heads, self.head_dim)

        outputs = (attn_output, present)
        if self.debug:
            if "qk" in DEBUG_OUTPUTS:
                outputs += (qk,)
            if "norm_qk" in DEBUG_OUTPUTS:
                outputs += (norm_qk,)
            if "softmax" in DEBUG_OUTPUTS:
                outputs += (softmax,)
            if "attn_weights" in DEBUG_OUTPUTS:
                outputs += (attn_weights,)

        return outputs

def create_inputs(batch_size=1, hidden_size=768, num_attention_heads=12, sequence_length=1, past_sequence_length=5, float16=False, device=torch.device('cuda'), padding_length=0):
    float_type = torch.float16 if float16 else torch.float32

    past_shape = [batch_size, num_attention_heads, past_sequence_length, int(hidden_size / num_attention_heads)]
    past_key = torch.rand(past_shape, dtype=float_type, device=device)
    past_value = torch.rand(past_shape, dtype=float_type, device=device)
    layer_past = MyGPT2Attention.concat_key_value(past_key, past_value) #(past_key, past_value)

    total_sequence_length = past_sequence_length + sequence_length

    attention_mask = torch.ones([batch_size, total_sequence_length], dtype=torch.int32, device=device)
    if padding_length > 0:
        padding_position = total_sequence_length - padding_length
        attention_mask[:, padding_position:] = 0
    elif padding_length < 0: # mask a random position
        for i in range(batch_size):
            padding_position = random.randint(0, total_sequence_length - 1)
            attention_mask[i, padding_position] = 0

    input_hidden_states = torch.normal(mean=0.0, std=0.1, size=(batch_size, sequence_length, hidden_size)).to(float_type).to(device)
    return input_hidden_states, attention_mask, layer_past

def get_output_names(debug=False):
    outputs = ["attn_output", "present"]
    if debug:
        outputs += DEBUG_OUTPUTS
    return outputs

def export_onnx(model, onnx_model_path, float16, hidden_size, num_attention_heads, debug=False):
    from pathlib import Path
    Path(onnx_model_path).parent.mkdir(parents=True, exist_ok=True)

    input_hidden_states, attention_mask, layer_past = create_inputs(float16=float16, hidden_size=hidden_size, num_attention_heads=num_attention_heads)

    with torch.no_grad():
        outputs = model(input_hidden_states, attention_mask=attention_mask, layer_past=layer_past)

    dynamic_axes = {'input_hidden_states': {0: 'batch_size', 1: 'seq_len'},
                    "attn_output": {0: 'batch_size', 1: 'seq_len'},
                    "past": {1: 'batch_size', 3: 'past_seq_len'},
                    "present": {1: 'batch_size', 3: 'total_seq_len'},
                    "attention_mask": {0: 'batch_size', 1: 'total_seq_len'}
                    }
    if debug:
        debug_dynamic_axes = {
                    "qk": {0: 'batch_size', 1: 'seq_len'},
                    "norm_qk": {0: 'batch_size', 1: 'seq_len'},
                    "softmax": {0: 'batch_size', 1: 'seq_len'},
                    "attn_weights": {0: 'batch_size', 1: 'seq_len'}}
        for name in DEBUG_OUTPUTS:             
            dynamic_axes[name] = debug_dynamic_axes[name]

    torch.onnx.export(model,
                      args=(input_hidden_states, {'attention_mask': attention_mask, 'layer_past': layer_past}),
                      f=onnx_model_path,
                      input_names=['input_hidden_states', 'attention_mask', 'past'],
                      output_names=get_output_names(debug),
                      dynamic_axes=dynamic_axes,
                      example_outputs=outputs,
                      opset_version=11,
                      do_constant_folding=True)
    print("exported:", onnx_model_path)

def optimize_onnx(input_onnx_path, optimized_onnx_path, num_heads=12, debug=False):
    from onnxruntime.transformers.onnx_model import OnnxModel
    m = onnx.load(input_onnx_path)
    onnx_model = OnnxModel(m)

    nodes_to_remove = onnx_model.nodes()
    output_names = ["attn_output", "present"] + DEBUG_OUTPUTS if debug else ["attn_output", "present"]
    node_to_add = helper.make_node("Attention", ["input_hidden_states", "c_attn.weight", "c_attn.bias", "attention_mask", "past"],
                         output_names,
                         "gpt2_attention",
                         num_heads=num_heads,
                         unidirectional=1,
                         domain="com.microsoft")

    onnx_model.remove_nodes(nodes_to_remove)
    onnx_model.add_node(node_to_add)
    onnx_model.prune_graph()
    onnx_model.save_model_to_file(optimized_onnx_path)

def diff_outputs(torch_outputs, ort_outputs, index, relative=False):
    """ Returns the maximum difference between PyTorch and OnnxRuntime outputs.
    """
    expected_outputs = torch_outputs[index].cpu().numpy()
    diff = numpy.abs(expected_outputs - ort_outputs[index])
    if relative:
        return numpy.amax(diff / (numpy.abs(expected_outputs) + 1e-6))
    else:
        return numpy.amax(diff)

def compare_outputs(torch_outputs, ort_outputs, rtol=1e-03, atol=1e-03, verbose=False):
    """ Returns True if torch and ORT outputs are close for given thresholds, and False otherwise.
    """
    is_all_close = True
    max_abs_diff = []
    for i in range(len(ort_outputs)):
        is_close = numpy.allclose(ort_outputs[i], torch_outputs[i].cpu().numpy(), rtol=rtol, atol=atol)
        if not is_close:
            is_all_close = False
            if verbose:
                print(f'output {i} ({ort_outputs[i].name}) are close: {is_close}')
        max_abs_diff.append(diff_outputs(torch_outputs, ort_outputs, i))
    
    if (not is_all_close) or max(max_abs_diff) > 0: #(verbose and max(max_abs_diff) > 0):
        messages = ["max_abs_diff per output:"]
        output_names = get_output_names(debug=True)
        for i, diff in enumerate(max_abs_diff):
            messages.append(f"{output_names[i]}={diff},")
        print(" ".join(messages))

    return is_all_close, max(max_abs_diff)

def onnxruntime_inference(onnx_model_path, input_hidden_states, attention_mask, past):
    from onnxruntime.transformers.benchmark_helper import create_onnxruntime_session
    ort_session = create_onnxruntime_session(onnx_model_path,
                                             use_gpu=True,
                                             enable_all_optimization=True,
                                             num_threads=-1,
                                             enable_profiling=False,
                                             verbose=False)

    ort_inputs = {
        'past': numpy.ascontiguousarray(past.cpu().numpy()),
        'attention_mask': numpy.ascontiguousarray(attention_mask.cpu().numpy()),
        'input_hidden_states': numpy.ascontiguousarray(input_hidden_states.cpu().numpy()),
    }

    ort_outputs = ort_session.run(None, ort_inputs)
    return ort_outputs

def verify_attention(model, onnx_model_path, batch_size=1, hidden_size=768, num_attention_heads=12, sequence_length=1, past_sequence_length=5, float16=False, device=torch.device('cuda'), padding_length=0, test_cases=100, optimized=False):
    print(f"optimized={optimized}, batch_size={batch_size}, hidden_size={hidden_size}, num_attention_heads={num_attention_heads}, sequence_length={sequence_length}, past_sequence_length={past_sequence_length}, float16={float16}, padding_length={padding_length}")
    passed_cases = 0
    max_diffs = []
    for i in range(test_cases):
        input_hidden_states, attention_mask, layer_past = create_inputs(batch_size, hidden_size, num_attention_heads, sequence_length, past_sequence_length, float16, device, padding_length)

        with torch.no_grad():
            torch_outputs = model(input_hidden_states, layer_past=layer_past, attention_mask=attention_mask)

        ort_outputs = onnxruntime_inference(onnx_model_path, input_hidden_states, attention_mask, layer_past)

        tolerance = 1e-03 if float16 else 1e-05
        is_all_close, max_diff = compare_outputs(torch_outputs, ort_outputs, rtol=tolerance, atol=tolerance)
        max_diffs.append(max_diff)
        if is_all_close:
            passed_cases += 1

    max_diff = max(max_diffs)
    diff_count = len([i for i in max_diffs if i > 0])
    print(f"Passed cases: {passed_cases}/{test_cases}; Max_diff={max_diff}; Diff_count={diff_count}")
    return test_cases - passed_cases

def run(batch_size=1, float16=False, optimized=False, hidden_size=768, num_attention_heads=12):
    test_name = f"batch_size={batch_size}, float16={float16}, optimized={optimized}, hidden_size={hidden_size}, num_attention_heads={num_attention_heads}"
    print(f"Testing ONNX parity: {test_name}")

    device=torch.device('cuda')
    debug=(DEBUG_OUTPUTS==["softmax"] or DEBUG_OUTPUTS==["qk"]) # assume that ORT will output same data for debugging.
    model = MyGPT2Attention(hidden_size=hidden_size, num_attention_heads=num_attention_heads, debug=debug)
    model.eval()
    model.to(device)
    if float16:
        model.half()

    # Do not re-use onnx file from previous test since weights of model are random.
    onnx_model_path = './temp/gpt_attention_{}.onnx'.format("fp16" if float16 else "fp32")
    export_onnx(model, onnx_model_path, float16, hidden_size, num_attention_heads, debug=debug)

    if optimized:
        optimized_onnx_path = './temp/gpt_attention_opt_{}.onnx'.format("fp16" if float16 else "fp32")
        optimize_onnx(onnx_model_path, optimized_onnx_path, num_attention_heads, debug=debug)
        onnx_path = optimized_onnx_path
    else:
        onnx_path = onnx_model_path

    # Test Case: No past state
    sequence_length=2
    past_sequence_length=0
    padding_length=0
    num_failure = 0
    num_failure += verify_attention(model, onnx_path, batch_size, hidden_size, num_attention_heads, sequence_length, past_sequence_length, float16, device, padding_length, optimized=optimized)

    # Test Case: with past state and padding last 2 words
    sequence_length=3
    past_sequence_length=5
    padding_length=2
    num_failure += verify_attention(model, onnx_path, batch_size, hidden_size, num_attention_heads, sequence_length, past_sequence_length, float16, device, padding_length, optimized=optimized)

    # Test Case: random mask one word
    sequence_length=1
    past_sequence_length=128
    padding_length=-1
    num_failure += verify_attention(model, onnx_path, batch_size, hidden_size, num_attention_heads, sequence_length, past_sequence_length, float16, device, padding_length, optimized=optimized)

    # clean up onnx file

    #os.remove(onnx_model_path)
    #if optimized:
    #    os.remove(onnx_path)

    return num_failure, test_name

class TestGptAttentionHuggingfaceParity(unittest.TestCase):
    def run_test(self, batch_size, float16, optimized, hidden_size, num_attention_heads):
        num_failure, test_name = run(batch_size, float16, optimized, hidden_size, num_attention_heads)
        self.assertTrue(num_failure==0, test_name)

    def run_small(self, optimized):
        for batch_size in [64]:
            self.run_test(batch_size, float16 = False, optimized=optimized, hidden_size=768, num_attention_heads=12)
            self.run_test(batch_size, float16 = True, optimized=optimized, hidden_size=768, num_attention_heads=12)

    def run_large(self, optimized):
        for batch_size in [2]:
            #self.run_test(batch_size, float16 = False, optimized=optimized, hidden_size=1024, num_attention_heads=16)
            #self.run_test(batch_size, float16 = True, optimized=optimized, hidden_size=1024, num_attention_heads=16)
            #self.run_test(batch_size, float16 = False, optimized=optimized, hidden_size=2048, num_attention_heads=16)
            #self.run_test(batch_size, float16 = True, optimized=optimized, hidden_size=2048, num_attention_heads=16)
            self.run_test(batch_size, float16 = False, optimized=optimized, hidden_size=4096, num_attention_heads=32)
            self.run_test(batch_size, float16 = True, optimized=optimized, hidden_size=4096, num_attention_heads=32)

    def test_optimized(self):
        self.run_small(True)

    def test_non_optimized(self):
        self.run_small(False)

    @pytest.mark.slow
    def test_optimized_large(self):
         self.run_large(True)

    @pytest.mark.slow
    def test_non_optimized_large(self):
        self.run_large(False)

if __name__ == '__main__':
    unittest.main()