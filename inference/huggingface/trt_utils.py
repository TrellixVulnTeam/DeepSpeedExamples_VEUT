#  Copyright 2022, Lefebvre Dalloz Services
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

"""
All the tooling to ease TensorRT usage.
"""

from typing import Callable, Dict, List, OrderedDict, Tuple

import tensorrt as trt
import torch
from time import time
from tensorrt import ICudaEngine, IExecutionContext
from tensorrt.tensorrt import (
    Builder,
    IBuilderConfig,
    IElementWiseLayer,
    ILayer,
    INetworkDefinition,
    IOptimizationProfile,
    IReduceLayer,
    Logger,
    OnnxParser,
    Runtime,
)


def fix_fp16_network(network_definition: INetworkDefinition) -> INetworkDefinition:
    """
    Mixed precision on TensorRT can generate scores very far from Pytorch because of some operator being saturated.
    Indeed, FP16 can't store very large and very small numbers like FP32.
    Here, we search for some patterns of operators to keep in FP32, in most cases, it is enough to fix the inference
    and don't hurt performances.
    :param network_definition: graph generated by TensorRT after parsing ONNX file (during the model building)
    :return: patched network definition
    """
    # search for patterns which may overflow in FP16 precision, we force FP32 precisions for those nodes
    for layer_index in range(network_definition.num_layers - 1):
        layer: ILayer = network_definition.get_layer(layer_index)
        next_layer: ILayer = network_definition.get_layer(layer_index + 1)
        # POW operation usually followed by mean reduce
        if layer.type == trt.LayerType.ELEMENTWISE and next_layer.type == trt.LayerType.REDUCE:
            # casting to get access to op attribute
            layer.__class__ = IElementWiseLayer
            next_layer.__class__ = IReduceLayer
            if layer.op == trt.ElementWiseOperation.POW:
                layer.precision = trt.DataType.FLOAT
                next_layer.precision = trt.DataType.FLOAT
            layer.set_output_type(index=0, dtype=trt.DataType.FLOAT)
            next_layer.set_output_type(index=0, dtype=trt.DataType.FLOAT)
    return network_definition


def build_engine(
    runtime: Runtime,
    onnx_file_path: str,
    logger: Logger,
    min_shape: Tuple[int, int],
    optimal_shape: Tuple[int, int],
    max_shape: Tuple[int, int],
    workspace_size: int,
    fp16: bool,
    int8: bool,
) -> ICudaEngine:
    """
    Convert ONNX file to TensorRT engine.
    It supports dynamic shape, however it's advised to keep sequence length fix as it hurts performance otherwise.
    Dynamic batch size don't hurt performance and is highly advised.
    :param runtime: global variable shared accross inference call / model building
    :param onnx_file_path: path to the ONNX file
    :param logger: specific logger to TensorRT
    :param min_shape: the minimal shape of input tensors. It's advised to set first dimension (batch size) to 1
    :param optimal_shape: input tensor shape used for optimizations
    :param max_shape: maximal input tensor shape
    :param workspace_size: GPU memory to use during the building, more is always better. If there is not enough memory,
    some optimization may fail, and the whole conversion process will crash.
    :param fp16: enable FP16 precision, it usually provide a 20-30% boost compared to ONNX Runtime.
    :param int8: enable INT-8 quantization, best performance but model should have been quantized.
    :return: TensorRT engine to use during inference
    """
    with trt.Builder(logger) as builder:  # type: Builder
        with builder.create_network(
            flags=1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH)
        ) as network_definition:  # type: INetworkDefinition
            with trt.OnnxParser(network_definition, logger) as parser:  # type: OnnxParser
                builder.max_batch_size = max_shape[0]  # max batch size
                config: IBuilderConfig = builder.create_builder_config()
                config.max_workspace_size = workspace_size
                # to enable complete trt inspector debugging, only for TensorRT >= 8.2
                # config.profiling_verbosity = trt.ProfilingVerbosity.DETAILED
                # disable CUDNN optimizations
                config.set_tactic_sources(
                    tactic_sources=1 << int(trt.TacticSource.CUBLAS) | 1 << int(trt.TacticSource.CUBLAS_LT)
                )
                if int8:
                    config.set_flag(trt.BuilderFlag.INT8)
                if fp16:
                    config.set_flag(trt.BuilderFlag.FP16)
                config.set_flag(trt.BuilderFlag.DISABLE_TIMING_CACHE)
                # https://github.com/NVIDIA/TensorRT/issues/1196 (sometimes big diff in output when using FP16)
                config.set_flag(trt.BuilderFlag.OBEY_PRECISION_CONSTRAINTS)
                logger.log(msg="parsing trt model", severity=trt.ILogger.WARNING)
                with open(onnx_file_path, "rb") as f:
                    # File path needed for models with external dataformat
                    parser.parse(model=f.read(), path=onnx_file_path)
                profile: IOptimizationProfile = builder.create_optimization_profile()
                for num_input in range(network_definition.num_inputs):
                    profile.set_shape(
                        input=network_definition.get_input(num_input).name,
                        min=min_shape,
                        opt=optimal_shape,
                        max=max_shape,
                    )
                config.add_optimization_profile(profile)
                if fp16:
                    network_definition = fix_fp16_network(network_definition)
                
                logger.log(msg="building engine. depending on model size this may take a while", severity=trt.ILogger.WARNING)
                t0 = time()
                trt_engine = builder.build_serialized_network(network_definition, config)
                engine: ICudaEngine = runtime.deserialize_cuda_engine(trt_engine)
                logger.log(msg=f"building engine took {time() - t0:4.1f} seconds", severity=trt.ILogger.WARNING)
                assert engine is not None, "error during engine generation, check error messages above :-("
                return engine


def get_output_tensors(
    context: trt.IExecutionContext,
    host_inputs: List[torch.Tensor],
    input_binding_idxs: List[int],
    output_binding_idxs: List[int],
) -> List[torch.Tensor]:
    """
    Reserve memory in GPU for input and output tensors.
    :param context: TensorRT context shared accross inference steps
    :param host_inputs: input tensor
    :param input_binding_idxs: indexes of each input vector (should be the same than during building)
    :param output_binding_idxs: indexes of each output vector (should be the same than during building)
    :return: tensors where output will be stored
    """
    # explicitly set dynamic input shapes, so dynamic output shapes can be computed internally
    for host_input, binding_index in zip(host_inputs, input_binding_idxs):
        context.set_binding_shape(binding_index, tuple(host_input.shape))
    assert context.all_binding_shapes_specified
    device_outputs: List[torch.Tensor] = []
    for binding_index in output_binding_idxs:
        # TensorRT computes output shape based on input shape provided above
        output_shape = context.get_binding_shape(binding_index)
        # allocate buffers to hold output results
        output = torch.empty(tuple(output_shape), device="cuda")
        device_outputs.append(output)
    return device_outputs


def infer_tensorrt(
    context: IExecutionContext,
    host_inputs: OrderedDict[str, torch.Tensor],
    input_binding_idxs: List[int],
    output_binding_idxs: List[int],
) -> List[torch.Tensor]:
    """
    Perform inference with TensorRT.
    :param context: shared variable
    :param host_inputs: input tensor
    :param input_binding_idxs: input tensor indexes
    :param output_binding_idxs: output tensor indexes
    :return: output tensor
    """
    input_tensors: List[torch.Tensor] = list()
    for tensor in host_inputs.values():
        assert isinstance(tensor, torch.Tensor), f"unexpected tensor type: {tensor.dtype}"
        # warning: small changes in output if int64 is used instead of int32
        tensor = tensor.type(torch.int32)
        tensor = tensor.to("cuda")
        input_tensors.append(tensor)
    # calculate input shape, bind it, allocate GPU memory for the output
    output_tensors: List[torch.Tensor] = get_output_tensors(
        context, input_tensors, input_binding_idxs, output_binding_idxs
    )
    bindings = [int(i.data_ptr()) for i in input_tensors + output_tensors]
    assert context.execute_async_v2(
        bindings, torch.cuda.current_stream().cuda_stream
    ), "failure during execution of inference"
    torch.cuda.current_stream().synchronize()  # sync all CUDA ops
    return output_tensors


def load_engine(
    runtime: Runtime, engine_file_path: str, profile_index: int = 0
) -> Callable[[Dict[str, torch.Tensor]], torch.Tensor]:
    """
    Load serialized TensorRT engine.
    :param runtime: shared variable
    :param engine_file_path: path to the serialized engine
    :param profile_index: which profile to load, 0 if you have not used multiple profiles
    :return: A function to perform inference
    """
    with open(file=engine_file_path, mode="rb") as f:
        engine: ICudaEngine = runtime.deserialize_cuda_engine(f.read())
        stream: int = torch.cuda.current_stream().cuda_stream
        context: IExecutionContext = engine.create_execution_context()
        context.set_optimization_profile_async(profile_index=profile_index, stream_handle=stream)
        # retrieve input/output IDs
        input_binding_idxs, output_binding_idxs = get_binding_idxs(engine, profile_index)  # type: List[int], List[int]

        def tensorrt_model(inputs: Dict[str, torch.Tensor]) -> torch.Tensor:
            return infer_tensorrt(
                context=context,
                host_inputs=inputs,
                input_binding_idxs=input_binding_idxs,
                output_binding_idxs=output_binding_idxs,
            )

        return tensorrt_model


def save_engine(engine: ICudaEngine, engine_file_path: str) -> None:
    """
    Serialize TensorRT engine to file.
    :param engine: TensorRT engine
    :param engine_file_path: output path
    """
    with open(engine_file_path, "wb") as f:
        f.write(engine.serialize())


def get_binding_idxs(engine: trt.ICudaEngine, profile_index: int):
    """
    Calculate start/end binding indices for current context's profile
    https://docs.nvidia.com/deeplearning/tensorrt/developer-guide/index.html#opt_profiles_bindings
    :param engine: TensorRT engine generated during the model building
    :param profile_index: profile to use (several profiles can be set during building)
    :return: input and output tensor indexes
    """
    num_bindings_per_profile = engine.num_bindings // engine.num_optimization_profiles
    start_binding = profile_index * num_bindings_per_profile
    end_binding = start_binding + num_bindings_per_profile  # Separate input and output binding indices for convenience
    input_binding_idxs: List[int] = []
    output_binding_idxs: List[int] = []
    for binding_index in range(start_binding, end_binding):
        if engine.binding_is_input(binding_index):
            input_binding_idxs.append(binding_index)
        else:
            output_binding_idxs.append(binding_index)
    return input_binding_idxs, output_binding_idxs