# Copyright (c) 2017 Sony Corporation. All Rights Reserved.
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

import os
import re
import numpy as np

import nnabla as nn
import nnabla.utils.callback as callback
from nnabla.utils.communicator_util import current_communicator
from nnabla.utils import nnabla_pb2
from nnabla.utils.get_file_handle import get_file_handle_save
from nnabla.logger import logger

cpu_load_backend_ok = True
try:
    import psutil
except Exception:
    # measure cpu load only if psutil installed
    cpu_load_backend_ok = False

gpu_load_backend_ok = True
try:
    import pynvml
    pynvml.nvmlInit()
except Exception:
    # measure gpu load only if nvml installed
    gpu_load_backend_ok = False


# load variable
# ============
gpu_m_count = 0
gpu_a_load = {}
if cpu_load_backend_ok:
    p_handler = psutil.Process()
    p_handler_avg = psutil.Process()
    p_handler_avg.cpu_percent()


def is_float(x):
    # x is string
    try:
        float(x)
        return True
    except ValueError:
        return False


def compute_full_path(root_path, file_path):
    full_path = os.path.join(root_path, file_path)
    full_path = full_path.replace('\\', os.path.sep)
    full_path = full_path.replace('/', os.path.sep)
    full_path = full_path.replace(os.path.sep + '.' + os.path.sep, os.path.sep)
    return full_path


def let_data_to_variable(variable, data, ctx=None, data_name=None, variable_name=None):
    try:
        if data.dtype <= np.float64:
            variable.data.cast(data.dtype)[...] = data
        else:
            variable.d = data
    except:
        if variable.shape != data.shape:
            logger.critical('Shape does not match between data{} and variable{} ({} != {}).'.format(
                ' "' + data_name + '"' if data_name else '',
                ' "' + variable_name + '"' if variable_name else '',
                data.shape, variable.shape))
        raise
    variable.need_grad = False

    # Copy to device
    if ctx:
        try:
            variable.data.cast(variable.data.dtype, ctx)
        except:
            if ctx.array_class != 'CpuArray':
                # Fallback to cpu
                ctx.array_class = 'CpuArray'
                variable.data.cast(variable.data.dtype, ctx)
            else:
                raise


def collect_and_shape_result(c_load, g_load):
    # c_load : float e.g. 58.5
    # g_load : [[nvidia_device_id, gpu_load]]

    comm = current_communicator()
    if comm:
        res = [[comm.rank, c_load], *g_load[:1]]
        t_load_ndarray = np.array(res).reshape(-1)

        load_var = nn.Variable([len(t_load_ndarray), ])
        load_var.d = t_load_ndarray
        load_list_var = [nn.Variable([len(t_load_ndarray), ])
                         for _ in range(comm.size)]
        comm.all_gather(load_var.data, [a.data for a in load_list_var])
        result_arr = [[*np.round(a.d.astype(float), decimals=1)]
                      for a in load_list_var]
    else:
        res = [[0, c_load], *g_load[:1]]
        t_load_ndarray = np.round(np.array(res).reshape(-1), decimals=1)
        result_arr = [[*t_load_ndarray.astype(float)]]

    result_arr = sorted(result_arr, key=lambda x: x[0])

    return result_arr


def measure_cpu_gpu_instant_load():
    # Get current cpu gpu load, as
    # load = [rank, cpu_load, nvidia_device_id, gpu_load]
    # result_arr: [load, load, ...]

    gpu_load = []
    if gpu_load_backend_ok:
        global gpu_a_load
        global gpu_m_count

        gpu_m_count += 1
        try:
            comm = current_communicator()
            if comm:
                index = comm.local_rank
            elif 'cuda' in str(nn.get_current_context().backend):
                index = 0
            else:
                raise Exception
            handler = pynvml.nvmlDeviceGetHandleByIndex(index)
            gpu_load = [
                [index, pynvml.nvmlDeviceGetUtilizationRates(handler).gpu]]

            if index in gpu_a_load.keys():
                gpu_a_load[index]['name'] = pynvml.nvmlDeviceGetName(
                    handler).decode("utf-8")
                o_load = gpu_a_load[index]['load']
                n_load = gpu_load[0][1]
                gpu_a_load[index]['load'] = (
                    (gpu_m_count - 1) * o_load + n_load) / gpu_m_count
            else:
                gpu_a_load[index] = {
                    'name': pynvml.nvmlDeviceGetName(handler).decode("utf-8"),
                    'load': gpu_load[0][1]
                }

        except Exception:
            gpu_load = []

    if cpu_load_backend_ok:
        global p_handler
        cpu_load = p_handler.cpu_percent()
        callback.update_status(
            ('cpu_gpu_load', collect_and_shape_result(cpu_load, gpu_load)))


def get_cpu_gpu_average_load():
    # Get average cpu gpu load, as
    # load = [rank, cpu_load, nvidia_device_id, gpu_load]
    # result_arr: [load, load, ...]

    g_load = []
    if gpu_load_backend_ok:
        global gpu_a_load
        global gpu_m_count

        load_info = {**gpu_a_load}
        gpu_a_load = {}
        gpu_m_count = 0

        # adjust data type then transfer them to numpy ndarray
        g_load = [[float(a), float(load_info[a]['load'])]
                  for a in load_info.keys()]
        g_load = g_load[:1] if len(g_load) else []

    if cpu_load_backend_ok:
        global p_handler_avg
        c_load = p_handler_avg.cpu_percent()
        return collect_and_shape_result(c_load, g_load)


def _create_optimizer_lite(opti):
    proto_o = nnabla_pb2.Optimizer()
    proto_o.name = opti.name
    proto_o.solver.type = re.sub(r'(|Cuda)$', '', str(opti.solver.name))
    opti.solver.set_states_to_protobuf(proto_o)
    return proto_o


def save_optimizer_states(filebase, ext, train_config):
    filelist = []
    if ext == '.protobuf':
        filename = filebase + '_optimizer.protobuf.optimizer'
        proto = nnabla_pb2.NNablaProtoBuf()
        proto_optimizers = []
        for o in train_config.optimizers.values():
            proto_optimizers.append(_create_optimizer_lite(o.optimizer))
        proto.optimizer.extend(proto_optimizers)
        with get_file_handle_save(filename, '.protobuf') as f:
            f.write(proto.SerializeToString())
            filelist.append(filename)
    else:
        for o in train_config.optimizers.values():
            f_name = '{}_{}_optimizer.h5'.format(
                o.optimizer.name,
                re.sub(r'(|Cuda)$', '', str(o.optimizer.solver.name))
            )
            filename = '{}_{}'.format(filebase, f_name)
            o.optimizer.solver.save_states(filename)
            name_ext = '{}.optimizer'.format(filename)
            os.rename(filename, name_ext)
            filelist.append(name_ext)
    return filelist
