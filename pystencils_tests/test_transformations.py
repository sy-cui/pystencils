import sympy as sp
import numpy as np

import pystencils as ps
from pystencils import fields, TypedSymbol
from pystencils.astnodes import LoopOverCoordinate, SympyAssignment
from pystencils.typing import create_type
from pystencils.transformations import (
    filtered_tree_iteration, get_loop_hierarchy, get_loop_counter_symbol_hierarchy,
    iterate_loops_by_depth, split_inner_loop, loop_blocking
)

from pystencils.cpu import add_pragmas

def test_loop_information():
    f, g = ps.fields("f, g: double[2D]")
    update_rule = ps.Assignment(g[0, 0], f[0, 0])

    ast = ps.create_kernel(update_rule)
    inner_loops = [loop for loop in filtered_tree_iteration(ast, LoopOverCoordinate, stop_type=SympyAssignment)
                   if loop.is_innermost_loop]

    loop_order = []
    for i in get_loop_hierarchy(inner_loops[0].args[0]):
        loop_order.append(i)

    assert loop_order == [0, 1]

    loop_symbols = get_loop_counter_symbol_hierarchy(inner_loops[0].args[0])

    assert loop_symbols == [TypedSymbol("ctr_1", create_type("int"), nonnegative=True),
                            TypedSymbol("ctr_0", create_type("int"), nonnegative=True)]


def test_iterate_loops_by_depth():
    f, g = ps.fields("f, g: double[3D]", layout="fzyx")
    x = ps.TypedSymbol('x', np.float64)
    subs = [ps.Assignment(x, f[0, 0, 0])]
    mains = [ps.Assignment(g[0, 0, 0], x)]
    ac = ps.AssignmentCollection(mains, subexpressions=subs)

    config = ps.CreateKernelConfig(cpu_blocking=(0, 16, 0))
    ast = ps.create_kernel(ac, config=config)
    split_inner_loop(ast, [[x], [g[0,0,0]]])

    loops = list(iterate_loops_by_depth(ast, 0))
    assert len(loops) == 1
    assert loops[0].loop_counter_symbol.name == "_blockctr_1"

    loops = list(iterate_loops_by_depth(ast, 1))
    assert len(loops) == 1
    assert loops[0].loop_counter_symbol.name == "ctr_2"

    loops = list(iterate_loops_by_depth(ast, 2))
    assert len(loops) == 1
    assert loops[0].loop_counter_symbol.name == "ctr_1"

    loops = list(iterate_loops_by_depth(ast, 3))
    assert len(loops) == 2
    assert loops[0].loop_counter_symbol.name == "ctr_0"
    assert loops[1].loop_counter_symbol.name == "ctr_0"

    innermost = list(iterate_loops_by_depth(ast, -1))
    assert loops == innermost


def test_split_optimisation():
    src, dst = fields(f"src(9), dst(9): [2D]", layout='fzyx')

    stencil = ((0, 0), (0, 1), (0, -1), (-1, 0), (1, 0), (-1, 1), (1, 1), (-1, -1), (1, -1))
    w = [sp.Rational(4, 9)]
    w += [sp.Rational(1, 9)] * 4
    w += [sp.Rational(1, 36)] * 4
    cs = sp.Rational(1, 3)

    subexpressions = []
    main_assignements = []

    rho = sp.symbols("rho")
    velo = sp.symbols("u_:2")

    density = 0
    velocity_x = 0
    velocity_y = 0
    for d in stencil:
        density += src[d]
        velocity_x += d[0] * src[d]
        velocity_y += d[1] * src[d]

    subexpressions.append(ps.Assignment(rho, density))
    subexpressions.append(ps.Assignment(velo[0], velocity_x))
    subexpressions.append(ps.Assignment(velo[1], velocity_y))

    for i, d in enumerate(stencil):
        u_d = velo[0] * d[0] + velo[1] * d[1]
        u_2 = velo[0] * velo[0] + velo[1] * velo[1]

        expr = w[i] * rho * (1 + u_d / cs + u_d ** 2 / (2 * cs ** 2) - u_2 / (2 * cs))

        main_assignements.append(ps.Assignment(dst.center_vector[i], expr))

    ac = ps.AssignmentCollection(main_assignments=main_assignements, subexpressions=subexpressions)

    simplification_hint = {'density': rho,
                           'velocity': (velo[0], velo[1]),
                           'split_groups': [[rho, velo[0], velo[1], dst.center_vector[0]],
                                            [dst.center_vector[1], dst.center_vector[2]],
                                            [dst.center_vector[3], dst.center_vector[4]],
                                            [dst.center_vector[5], dst.center_vector[6]],
                                            [dst.center_vector[7], dst.center_vector[8]]]}

    ac.simplification_hints = simplification_hint
    ast = ps.create_kernel(ac)

    code = ps.get_code_str(ast)
    # after the split optimisation the two for loops are split into 6
    assert code.count("for") == 6

    print(code)

def test_pragmas():
    f, g = ps.fields("f, g: double[3D]", layout="fzyx")
    x = ps.TypedSymbol('x', np.float64)
    subs = [ps.Assignment(x, f[0, 0, 0])]
    mains = [ps.Assignment(g[0, 0, 0], x)]
    ac = ps.AssignmentCollection(mains, subexpressions=subs)

    def prepend_omp_pragmas(ast):
        add_pragmas(ast, ["#pragma omp for schedule(dynamic)"], nesting_depth=0)
        add_pragmas(ast, ["#pragma omp simd simdlen(8)"], nesting_depth=-1)

    ast_passes = [prepend_omp_pragmas]

    config = ps.CreateKernelConfig(target=ps.Target.CPU, cpu_prepend_optimizations=ast_passes)
    ast = ps.create_kernel(ac, config=config)
    code = ps.get_code_str(ast)

    assert code.find("#pragma omp for schedule(dynamic)") != -1
    assert code.find("#pragma omp simd simdlen(8)") != -1

    loops = [loop for loop in filtered_tree_iteration(ast, LoopOverCoordinate, stop_type=SympyAssignment)]
    
    innermost = list(filter(lambda n: n.is_innermost_loop, loops))
    assert innermost[0].prefix_lines == ["#pragma omp simd simdlen(8)"]
    
    outermost = list(filter(lambda n: n.is_outermost_loop, loops))
    assert outermost[0].prefix_lines == ["#pragma omp for schedule(dynamic)"]
