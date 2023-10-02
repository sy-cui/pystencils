import pytest

import numpy as np
import cupy as cp
import sympy as sp
from scipy.ndimage import convolve

from pystencils import Assignment, Field, fields, CreateKernelConfig, create_kernel, Target
from pystencils.gpu import BlockIndexing
from pystencils.simp import sympy_cse_on_assignment_list
from pystencils.slicing import add_ghost_layers, make_slice, remove_ghost_layers, normalize_slice

try:
    import cupy
    device_numbers = range(cupy.cuda.runtime.getDeviceCount())
except ImportError:
    device_numbers = []


def test_averaging_kernel():
    size = (40, 55)
    src_arr = np.random.rand(*size)
    src_arr = add_ghost_layers(src_arr)
    dst_arr = np.zeros_like(src_arr)
    src_field = Field.create_from_numpy_array('src', src_arr)
    dst_field = Field.create_from_numpy_array('dst', dst_arr)

    update_rule = Assignment(dst_field[0, 0],
                             (src_field[0, 1] + src_field[0, -1] + src_field[1, 0] + src_field[-1, 0]) / 4)

    config = CreateKernelConfig(target=Target.GPU)
    ast = create_kernel(sympy_cse_on_assignment_list([update_rule]), config=config)
    kernel = ast.compile()

    gpu_src_arr = cp.asarray(src_arr)
    gpu_dst_arr = cp.asarray(dst_arr)
    kernel(src=gpu_src_arr, dst=gpu_dst_arr)
    dst_arr = gpu_dst_arr.get()

    stencil = np.array([[0, 1, 0], [1, 0, 1], [0, 1, 0]]) / 4.0
    reference = convolve(remove_ghost_layers(src_arr), stencil, mode='constant', cval=0.0)
    reference = add_ghost_layers(reference)
    np.testing.assert_almost_equal(reference, dst_arr)


def test_variable_sized_fields():
    src_field = Field.create_generic('src', spatial_dimensions=2)
    dst_field = Field.create_generic('dst', spatial_dimensions=2)

    update_rule = Assignment(dst_field[0, 0],
                             (src_field[0, 1] + src_field[0, -1] + src_field[1, 0] + src_field[-1, 0]) / 4)

    config = CreateKernelConfig(target=Target.GPU)
    ast = create_kernel(sympy_cse_on_assignment_list([update_rule]), config=config)
    kernel = ast.compile()

    size = (3, 3)
    src_arr = np.random.rand(*size)
    src_arr = add_ghost_layers(src_arr)
    dst_arr = np.zeros_like(src_arr)

    gpu_src_arr = cp.asarray(src_arr)
    gpu_dst_arr = cp.asarray(dst_arr)
    kernel(src=gpu_src_arr, dst=gpu_dst_arr)
    dst_arr = gpu_dst_arr.get()

    stencil = np.array([[0, 1, 0], [1, 0, 1], [0, 1, 0]]) / 4.0
    reference = convolve(remove_ghost_layers(src_arr), stencil, mode='constant', cval=0.0)
    reference = add_ghost_layers(reference)
    np.testing.assert_almost_equal(reference, dst_arr)


def test_multiple_index_dimensions():
    """Sums along the last axis of a numpy array"""
    src_size = (7, 6, 4)
    dst_size = src_size[:2]
    src_arr = np.asfortranarray(np.random.rand(*src_size))
    dst_arr = np.zeros(dst_size)

    src_field = Field.create_from_numpy_array('src', src_arr, index_dimensions=1)
    dst_field = Field.create_from_numpy_array('dst', dst_arr, index_dimensions=0)

    offset = (-2, -1)
    update_rule = Assignment(dst_field[0, 0],
                             sum([src_field[offset[0], offset[1]](i) for i in range(src_size[-1])]))

    config = CreateKernelConfig(target=Target.GPU)
    ast = create_kernel([update_rule], config=config)
    kernel = ast.compile()

    gpu_src_arr = cp.asarray(src_arr)
    gpu_dst_arr = cp.asarray(dst_arr)
    kernel(src=gpu_src_arr, dst=gpu_dst_arr)
    dst_arr = gpu_dst_arr.get()

    reference = np.zeros_like(dst_arr)
    gl = np.max(np.abs(np.array(offset, dtype=int)))
    for x in range(gl, src_size[0]-gl):
        for y in range(gl, src_size[1]-gl):
            reference[x, y] = sum([src_arr[x+offset[0], y+offset[1], i] for i in range(src_size[2])])

    np.testing.assert_almost_equal(reference, dst_arr)


def test_ghost_layer():
    size = (6, 5)
    src_arr = np.ones(size)
    dst_arr = np.zeros_like(src_arr)
    src_field = Field.create_from_numpy_array('src', src_arr, index_dimensions=0)
    dst_field = Field.create_from_numpy_array('dst', dst_arr, index_dimensions=0)

    update_rule = Assignment(dst_field[0, 0], src_field[0, 0])
    ghost_layers = [(1, 2), (2, 1)]

    config = CreateKernelConfig(target=Target.GPU, ghost_layers=ghost_layers, gpu_indexing="line")
    ast = create_kernel(sympy_cse_on_assignment_list([update_rule]), config=config)
    kernel = ast.compile()

    gpu_src_arr = cp.asarray(src_arr)
    gpu_dst_arr = cp.asarray(dst_arr)
    kernel(src=gpu_src_arr, dst=gpu_dst_arr)
    dst_arr = gpu_dst_arr.get()

    reference = np.zeros_like(src_arr)
    reference[ghost_layers[0][0]:-ghost_layers[0][1], ghost_layers[1][0]:-ghost_layers[1][1]] = 1
    np.testing.assert_equal(reference, dst_arr)


def test_setting_value():
    arr_cpu = np.arange(25, dtype=np.float64).reshape(5, 5)
    arr_gpu = cp.asarray(arr_cpu)

    iteration_slice = make_slice[:, :]
    f = Field.create_generic("f", 2)
    update_rule = [Assignment(f(0), sp.Symbol("value"))]

    config = CreateKernelConfig(target=Target.GPU, gpu_indexing="line", iteration_slice=iteration_slice)
    ast = create_kernel(sympy_cse_on_assignment_list(update_rule), config=config)
    kernel = ast.compile()

    kernel(f=arr_gpu, value=np.float64(42.0))
    np.testing.assert_equal(arr_gpu.get(), np.ones((5, 5)) * 42.0)


def test_periodicity():
    from pystencils.gpu.periodicity import get_periodic_boundary_functor as periodic_gpu
    from pystencils.slicing import get_periodic_boundary_functor as periodic_cpu

    arr_cpu = np.arange(50, dtype=np.float64).reshape(5, 5, 2)
    arr_gpu = cp.asarray(arr_cpu)

    periodicity_stencil = [(1, 0), (-1, 0), (1, 1)]
    periodic_gpu_kernel = periodic_gpu(periodicity_stencil, (5, 5), 1, 2)
    periodic_cpu_kernel = periodic_cpu(periodicity_stencil)

    cpu_result = np.copy(arr_cpu)
    periodic_cpu_kernel(cpu_result)

    periodic_gpu_kernel(pdfs=arr_gpu)
    gpu_result = arr_gpu.get()
    np.testing.assert_equal(cpu_result, gpu_result)


@pytest.mark.parametrize("device_number", device_numbers)
def test_block_indexing(device_number):
    f = fields("f: [3D]")
    s = normalize_slice(make_slice[:, :, :], f.spatial_shape)
    bi = BlockIndexing(s, f.layout, block_size=(16, 8, 2),
                       permute_block_size_dependent_on_layout=False)
    assert bi.call_parameters((3, 2, 32))['block'] == (3, 2, 32)
    assert bi.call_parameters((32, 2, 32))['block'] == (16, 2, 8)

    bi = BlockIndexing(s, f.layout, block_size=(32, 1, 1),
                       permute_block_size_dependent_on_layout=False)
    assert bi.call_parameters((1, 16, 16))['block'] == (1, 16, 2)

    bi = BlockIndexing(s, f.layout, block_size=(16, 8, 2),
                       maximum_block_size="auto", device_number=device_number)

    # This function should be used if number of needed registers is known. Can be determined with func.num_regs
    registers_per_thread = 1000
    blocks = bi.limit_block_size_by_register_restriction([1024, 1024, 1], registers_per_thread)

    if cp.cuda.runtime.is_hip:
        max_registers_per_block = cp.cuda.runtime.deviceGetAttribute(71, device_number)
    else:
        device = cp.cuda.Device(device_number)
        da = device.attributes
        max_registers_per_block = da.get("MaxRegistersPerBlock")

    assert np.prod(blocks) * registers_per_thread < max_registers_per_block


@pytest.mark.parametrize('gpu_indexing', ("block", "line"))
@pytest.mark.parametrize('layout', ("C", "F"))
@pytest.mark.parametrize('shape', ((5, 5, 5, 5), (3, 17, 387, 4), (23, 44, 21, 11)))
def test_four_dimensional_kernel(gpu_indexing, layout, shape):
    n_elements = np.prod(shape)

    arr_cpu = np.arange(n_elements, dtype=np.float64).reshape(shape, order=layout)
    arr_gpu = cp.asarray(arr_cpu)

    iteration_slice = make_slice[:, :, :, :]
    f = Field.create_from_numpy_array("f", arr_cpu)
    update_rule = [Assignment(f.center, sp.Symbol("value"))]

    config = CreateKernelConfig(target=Target.GPU, gpu_indexing=gpu_indexing, iteration_slice=iteration_slice)
    ast = create_kernel(update_rule, config=config)
    kernel = ast.compile()

    kernel(f=arr_gpu, value=np.float64(42.0))
    np.testing.assert_equal(arr_gpu.get(), np.ones(shape) * 42.0)
