# Copyright 2022 The TensorFlow Authors. All Rights Reserved.
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
# ==============================================================================
"""Propagates information about tensor layouts across operations."""

import contextlib
import logging
import os
import threading
from typing import List, Set

import numpy as np

# pylint: disable=g-direct-tensorflow-import
from tensorflow.core.framework import attr_value_pb2
from tensorflow.dtensor.python import _dtensor_device
from tensorflow.dtensor.python import gen_dtensor_ops
from tensorflow.dtensor.python import layout as layout_lib
from tensorflow.python.eager import context
from tensorflow.python.eager import core
from tensorflow.python.framework import device as tf_device
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import ops
from tensorflow.python.framework import sparse_tensor
from tensorflow.python.ops import resource_variable_ops

_DT_CLIENT_ID = "DTENSOR_CLIENT_ID"
_DT_NUM_CLIENTS = "DTENSOR_NUM_CLIENTS"
_DT_JOB_NAME = "DTENSOR_JOB_NAME"

# TODO(allenl): Allow something other than "CUSTOM" so we don't need device
# numbering hacks to avoid collisions between parallel devices and dtensor
# devices.
_next_device_number = 0
_next_device_number_lock = threading.Lock()


class DTensorDevice(object):
  """Wraps a custom device which attempts to propagate tensor layouts."""

  def __init__(self, meshes: List[layout_lib.Mesh], is_async=True):
    """Create a new DTensorDevice which executes ops on `underlying_device`.

    Args:
      meshes: A list of `Mesh` objects indicating groups of devices to execute
        on. These may also be registered lazily.
      is_async: Indicates whether DTensor operations on this client will return
        immediately (with "non-ready" handles) or block until executed. This is
        on by default and is exposed as an option for ease of debugging.
    """
    if any(not isinstance(mesh, layout_lib.Mesh) for mesh in meshes):
      raise TypeError(
          "Expected a flat list of Mesh objects, got {}".format(meshes))
    global _next_device_number, _next_device_number_lock
    ctx = context.context()
    with _next_device_number_lock:
      self.name = "{}/device:CUSTOM:{}".format(ctx.host_address_space(),
                                               _next_device_number)
      _next_device_number += 1
    device, device_info = _dtensor_device.Allocate(self.name)
    context.register_custom_device(device, self.name, device_info)

    self._device_info = device_info
    self._current_output_layout = None
    self._is_async = is_async
    self._meshes = set()
    self._mesh_lock = threading.Lock()
    for mesh in meshes:
      self._register_mesh(mesh)

# LINT.IfChange
  def _num_clients(self):
    """Returns number of clients in current DTensor cluster."""
    # If missing, 1 is a good default.
    return int(os.environ.get(_DT_NUM_CLIENTS, "1"))
# LINT.ThenChange(//tensorflow/dtensor/cc/dtensor_utils.cc)

# LINT.IfChange
  def _client_id(self):
    """Returns current client ID (int) in current DTensor cluster."""
    return int(os.environ.get(_DT_CLIENT_ID, "0"))
# LINT.ThenChange(//tensorflow/dtensor/cc/dtensor_utils.cc)

  def _job_name(self):
    """Returns the DTensor Borg job name."""
    # If missing, the program is likely running locally or on Forge.
    return os.environ.get(_DT_JOB_NAME, "localhost")

  def _full_job_name(self):
    """Returns the fully qualified TF job name for this or another task."""
    if self._job_name() == "localhost":
      return "localhost/replica:0/task:0"
    return self._job_name() + "/replica:0/task:" + str(self._client_id())

  def _create_host_array(self, shape, host_id):
    """Returns ID and device lists that can be used to create a host mesh."""
    num_global_devices = np.prod(shape)
    global_device_ids = np.arange(num_global_devices).reshape(shape)
    local_device_list = [
        tf_device.DeviceSpec(
            job=self._full_job_name(), device_type="CPU", device_index=0)
    ]
    num_local_devices = len(local_device_list)
    local_device_ids = [
        x + host_id * num_local_devices for x in range(num_local_devices)
    ]
    return global_device_ids, local_device_ids, local_device_list

  def _create_embedding_host_mesh(self, tpu_mesh: layout_lib.Mesh):
    """Returns Embedding host mesh for each client."""
    if tpu_mesh.device_type().upper() != "TPU":
      raise ValueError("Must pass input of a tpu mesh.")

    # Global device ids are global host ids, while local device ids contains
    # local host id.

    ts_local_device_ids = []
    ts_local_devices = []
    for local_device_str in tpu_mesh.local_devices():
      # We only need to keep TPU:0 for each client.
      if not local_device_str.endswith("TPU:0"):
        continue

      device_spec = tf_device.DeviceSpec.from_string(local_device_str)
      ts_local_device_ids.append(device_spec.task)
      ts_local_devices.append(device_spec.replace(device_type="CPU"))

    if not ts_local_device_ids or not ts_local_device_ids:
      logging.info(
          "Cannot create tpu system mesh as %s has no `TPU:0` local device "
          "found", tpu_mesh.to_string())
      return None

    ts_global_device_ids = np.arange(self._num_clients())
    # TODO(zhonglinhan): parse global device specs as input when not None.
    return layout_lib.Mesh(
        dim_names=[tpu_mesh.dim_names[0]],  # 1D mesh.
        global_device_ids=ts_global_device_ids,
        local_device_ids=ts_local_device_ids,
        local_devices=ts_local_devices)

  def _register_mesh(self, mesh: layout_lib.Mesh):
    """Idempotently register `mesh` with the dtensor device."""
    with self._mesh_lock:
      if mesh not in self._meshes:
        _dtensor_device.AddMesh(self._device_info, mesh.to_string(),
                                self._is_async, False)
        self._meshes.add(mesh)
        if mesh.device_type().upper() == "TPU":
          logging.info(
              "Registering virtual 1:1 mapped host mesh %s for mesh %s",
              mesh.host_mesh().to_string(), mesh.to_string())
          _dtensor_device.AddMesh(self._device_info,
                                  mesh.host_mesh().to_string(), self._is_async,
                                  True)
          self._meshes.add(mesh.host_mesh())
          embedding_host_mesh = self._create_embedding_host_mesh(mesh)
          if embedding_host_mesh:
            logging.info(
                "Registering embedding host mesh %s on each client for mesh %s ",
                embedding_host_mesh.to_string(), mesh.to_string())
            _dtensor_device.AddMesh(self._device_info,
                                    embedding_host_mesh.to_string(),
                                    self._is_async, False)
            self._meshes.add(embedding_host_mesh)

  @property
  def meshes(self) -> Set[layout_lib.Mesh]:
    return self._meshes

  def copy_to_mesh(self, tensor, new_layout, source_layout=None) -> ops.Tensor:
    """Copy `tensor` to `device` with the given layout."""
    self._register_mesh(new_layout.mesh)
    with ops.device(self.name):
      return gen_dtensor_ops.copy_to_mesh(
          tensor,
          layout=new_layout.to_string(),
          source_layout=source_layout.to_string() if source_layout else "")

  # pylint: disable=g-doc-return-or-yield
  # pylint: disable=g-doc-args
  # TODO(xiejw): add args and return once LG.
  def pack(self, tensors, layout):
    """Returns a packed tensor handle for dtensor_device.

    Packing and unpacking are inverse operations:

    * unpack(pack(tensors)) == tensors
    * pack(unpack(dtensor)) == dtensor

    1. For any DTensor on the mesh, `unpack` returns the raw components placed
       on each underlying device.
    2. Packing these raw components in the same order returns a DTensor which
       should be identical to the original DTensor--both the content value and
       the layout.

    N.B. It is the callers responsibility to ensure that the underlying values
    for Pack() adhere to the specified layout. See examples below for more
    detail.

    N.B. [Shape, Rank, and Scalar]: The rank of the DTensor is the same as the
    rank of it's raw components, i.e., rank is preserved.  This leads to a
    consistent interpretation for packing scalar values into a DTensor. The only
    valid layout for a scalar value is fully replicated, and the individual
    components must be identical scalars.

    N.B. Each input `tensor[i]` will be copied to `layout.mesh.local_device[i]`
    if not already on the local device.

    For example, assume we have a mesh [X(2), Y(3)], which has in total 6
    underlying devices. Futuremore, assume that the device location mapping is
    the following:

       device_ID  |  location X, Y
               0     0, 0
               1     0, 1
               2     0, 2
               3     1, 0
               4     1, 1
               5     1, 2


    1. For 1-D vector DTensor with shape [128] with layout [mesh.X] and value as
       range(128), the raw components will have shape [64] each, and the raw
       components will be:

       device_ID  |  raw component
               0     range(64)
               1     range(64)
               2     range(64)
               3     range(64, 128)
               4     range(64, 128)
               5     range(64, 128)


       This also means for a 1-D DTensor with shape [2] and layout [mesh.X], the
       raw components have shape [1] rather than the shape for scalar value.

    2. For 2-D vector DTensor with shape [2, 3] with layout [mesh.X, mesh.Y] and
       value as range(6), this is basically a fully-sharded DTensor.

       From global view, the content looks like
           [
             [0.0, 1.0, 2.0],
             [3.0, 4.0, 5.0],
           ]

       The raw components will have shape [1, 1] each, and have the following
       content:

       device_ID  |  raw component
               0     [[0.0]]
               1     [[1.0]]
               2     [[2.0]]
               3     [[3.0]]
               4     [[4.0]]
               5     [[5.0]]

    3. For a scalar value 123.0 DTensor, it can only have one legitimate layout
       `[]` (no dimension, but fully replicated).

       The raw components will have shape [] each, and have the following
       content:

       device_ID  |  raw component
               0     123.0
               1     123.0
               2     123.0
               3     123.0
               4     123.0
               5     123.0

       Again, caller of `pack` is expected to provide 6 identical value raw
       components with scalar shapes.

    4. For 3-D vector DTensor with shape [2, 2, 3] with layout
       [X, unsharded, unsharded] and value as range(12),

       From global view, the content looks like
           [
             [
               [0.0, 1.0, 2.0],
               [3.0, 4.0, 5.0],
             ],
             [
               [6.0, 7.0, 8.0],
               [9.0, 10., 11.],
             ],
           ]

       The raw components will have shape [1, 2, 3] each, and have the following
       content:

       device_ID  |  raw component
               0     range(6).reshape([1, 2, 3])
               1     range(6).reshape([1, 2, 3])
               2     range(6).reshape([1, 2, 3])
               3     range(6, 12).reshape([1, 2, 3])
               4     range(6, 12).reshape([1, 2, 3])
               5     range(6, 12).reshape([1, 2, 3])

    Raises:
      RuntimeError: When not called eagerly.
    """
    if not context.executing_eagerly():
      raise RuntimeError("Pack must be called eagerly.")
    if any(
        issubclass(type(t), resource_variable_ops.BaseResourceVariable)
        for t in tensors):
      raise TypeError(
          "Received Variable input to Pack, Variable is not supported.")
    self._register_mesh(layout.mesh)
    with ops.device(self.name):
      if all(isinstance(t, sparse_tensor.SparseTensor) for t in tensors):
        if not all(t.shape == tensors[0].shape for t in tensors):
          raise TypeError("All input SparseTensors to Pack must be same shape.")
        is_sparse = True
        tensors = [t.indices for t in tensors] + [t.values for t in tensors] + [
            ops.convert_to_tensor(t.shape, dtype=dtypes.int64) for t in tensors
        ]
      elif any(isinstance(t, sparse_tensor.SparseTensor) for t in tensors):
        raise TypeError("Cannot Pack SparseTensors with Tensors.")
      else:
        is_sparse = False
      try:
        return _dtensor_device.Pack(
            context.context()._handle,  # pylint: disable=protected-access
            tensors,
            layout.to_string(),
            self._device_info,
            is_sparse)
      except core._NotOkStatusException as e:  # pylint: disable=protected-access
        raise core._status_to_exception(e) from None  # pylint: disable=protected-access

  def unpack(self, tensor):
    """Returns the raw tensor components of the packed tensor.

    Packing and unpacking are inverse operations:

    * unpack(pack(tensors)) == tensors
    * pack(unpack(dtensor)) == dtensor

    See documentation for pack for more details.

    Raises:
      RuntimeError: When not called eagerly.
    """
    if not context.executing_eagerly():
      raise RuntimeError("Unpack must be called eagerly.")
    if issubclass(type(tensor), resource_variable_ops.BaseResourceVariable):
      raise TypeError(
          "Received Variable input to unpack, Variable is not supported.")
    try:
      tensors = _dtensor_device.Unpack(
          context.context()._handle,  # pylint: disable=protected-access
          tensor,
          self._device_info)
    except core._NotOkStatusException as e:  # pylint: disable=protected-access
      raise core._status_to_exception(e) from None  # pylint: disable=protected-access

    is_sparse = _dtensor_device.IsSparseDTensor(
        context.context()._handle,  # pylint: disable=protected-access.
        tensor,
        self._device_info)
    if is_sparse:
      result = []
      for i in range(len(tensors) // 3):
        result.append(
            sparse_tensor.SparseTensor(tensors[i],
                                       tensors[i + len(tensors) // 3],
                                       tensors[i + 2 * len(tensors) // 3]))
      return result
    else:
      return tensors

  def fetch_layout(self, tensor):
    """Returns the layout of tensor.

    Raises:
      RuntimeError: When not called eagerly.
    """
    if not context.executing_eagerly():
      raise RuntimeError("FetchLayout must be called eagerly.")
    if issubclass(type(tensor), resource_variable_ops.BaseResourceVariable):
      tensor = tensor.read_value()
    try:
      layout_string = _dtensor_device.FetchLayout(
          context.context()._handle,  # pylint: disable=protected-access
          tensor,
          self._device_info)
    except core._NotOkStatusException as e:  # pylint: disable=protected-access
      raise core._status_to_exception(e) from None  # pylint: disable=protected-access
    return layout_lib.Layout.from_string(layout_string)

  def set_same_shape_policy(self, enabled):
    """Guess layouts using the layouts of other tensors with the same shape.

    This is the default behavior, and is quite safe. The `default_layout` scope
    overrides shape-based guesses.

    Args:
      enabled: A boolean indicating whether to use the policy.
    """
    _dtensor_device.SetSameShapePolicy(self._device_info, enabled)

  def set_tpu_core_ids(self, mesh_name, tpu_core_ids):
    """Sets the singleton global device ID-to-physical core ID map.

    Args:
      mesh_name: The name of a mesh. If empty, set the default mapping.
      tpu_core_ids: TPU core IDs sorted by TF task/device ordinal.
    """
    _dtensor_device.SetTPUCoreIDs(self._device_info, mesh_name, tpu_core_ids)

  def clear_tpu_core_ids(self):
    _dtensor_device.ClearTPUCoreIDs(self._device_info)

  def tpu_core_ids_to_locations(self, tpu_core_ids):
    """Translates TPU core IDs to TPU core locations.

    Args:
      tpu_core_ids: A list of TPU core IDs. Each one is an unsigned integer.

    Returns:
      A list of corresponding TPU core locations.
    """
    return _dtensor_device.TPUCoreIDsToLocations(
        context.context()._handle,  # pylint: disable=protected-access
        self._device_info,
        tpu_core_ids)

  def tpu_core_locations_to_ids(self, tpu_core_locations):
    """Translates TPU core locations to TPU core IDs.

    Args:
      tpu_core_locations: A list of TPU core locations. Each one is a list of
        four unsigned integers, [x, y, z, core].

    Returns:
      A list of corresponding TPU core IDs.
    """
    return _dtensor_device.TPUCoreLocationsToIDs(
        context.context()._handle,  # pylint: disable=protected-access
        self._device_info,
        tpu_core_locations)

  @contextlib.contextmanager
  def _experimental_default_mesh(self, mesh: layout_lib.Mesh):
    self._register_mesh(mesh)
    _dtensor_device.ExperimentalSetDefaultMesh(self._device_info,
                                               mesh.to_string().encode("utf-8"))
    yield
    _dtensor_device.ExperimentalClearDefaultMesh(self._device_info)

  @contextlib.contextmanager
  def _default_layout(self, layout: layout_lib.Layout):
    """Sets a default output layout for all ops in the scope.

    Note: This is an internal helper method, which is not user facing api.

    Useful for requesting a specific layout for ops which would have no inferred
    layout, e.g. tf.zeros.

    Caveats:

    - Currently only affects the first output of an op. For Op with multiple
      outputs, this does not support yet.

    - All Ops in the scope will be attached with the same layout. This might not
      be valid as the rank is different. The current suggestion is: Try to wrap
      the raw op wheneven possible.

    Args:
      layout: A Layout for the outputs of all operations in this scope.

    Yields:
      Nothing.
    """
    self._register_mesh(layout.mesh)
    try:
      previous_default = self._current_output_layout
      self._current_output_layout = layout.to_string().encode("utf-8")
      _dtensor_device.ExperimentalSetDefaultLayout(self._device_info,
                                                   self._current_output_layout)
      if context.executing_eagerly():
        graph = None
        previous_graph_size = None
        with ops.device(self.name):
          yield
      else:
        # Custom devices currently don't affect graph building, so we need a
        # separate way to indicate layouts.
        #
        # TODO(allenl): Remove this case once the DTensor device is active
        # during tracing.
        graph = ops.get_default_graph()
        previous_graph_size = len(graph.get_operations())
        yield
    finally:
      if graph is not None:  # pytype: disable=name-error  # py39-upgrade
        # Tag operations added under this scope
        for operation in graph.get_operations()[previous_graph_size:]:  # pytype: disable=name-error  # py39-upgrade
          # Set layout directly on the Op itself.
          operation._set_attr(  # pylint: disable=protected-access
              "_layout",
              attr_value_pb2.AttrValue(
                  list=attr_value_pb2.AttrValue.ListValue(
                      s=[self._current_output_layout])))
          operation._set_attr(  # pylint: disable=protected-access
              "_mesh",
              attr_value_pb2.AttrValue(
                  s=layout.mesh.to_string().encode("utf-8")))

      self._current_output_layout = previous_default  # pytype: disable=name-error  # py39-upgrade
      if self._current_output_layout is None:
        _dtensor_device.ExperimentalClearDefaultLayout(self._device_info)
      else:
        _dtensor_device.ExperimentalSetDefaultLayout(
            self._device_info, self._current_output_layout.decode("utf-8"))
