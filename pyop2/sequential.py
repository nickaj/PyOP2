# This file is part of PyOP2
#
# PyOP2 is Copyright (c) 2012, Imperial College London and
# others. Please see the AUTHORS file in the main source directory for
# a full list of copyright holders.  All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * The name of Imperial College London or that of other
#       contributors may not be used to endorse or promote products
#       derived from this software without specific prior written
#       permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTERS
# ''AS IS'' AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT HOLDERS OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT,
# INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED
# OF THE POSSIBILITY OF SUCH DAMAGE.

"""OP2 sequential backend."""
from __future__ import absolute_import, print_function, division
from six.moves import range, zip

import os
from textwrap import dedent
from copy import deepcopy as dcopy
from collections import OrderedDict

from pyop2.datatypes import IntType, as_cstr, as_ctypes
from pyop2 import base
from pyop2 import compilation
from pyop2 import petsc_base
from pyop2.base import par_loop                          # noqa: F401
from pyop2.base import READ, WRITE, RW, INC, MIN, MAX    # noqa: F401
from pyop2.base import ON_BOTTOM, ON_TOP, ON_INTERIOR_FACETS, ALL
from pyop2.base import Map, MixedMap, DecoratedMap, Sparsity, Halo  # noqa: F401
from pyop2.base import Set, ExtrudedSet, MixedSet, Subset, LocalSet  # noqa: F401
from pyop2.base import DatView                           # noqa: F401
from pyop2.petsc_base import DataSet, MixedDataSet       # noqa: F401
from pyop2.petsc_base import Global, GlobalDataSet       # noqa: F401
from pyop2.petsc_base import Dat, MixedDat, Mat          # noqa: F401
from pyop2.configuration import configuration
from pyop2.exceptions import *  # noqa: F401
from pyop2.mpi import collective
from pyop2.profiling import timed_region
from pyop2.utils import as_tuple, cached_property, strip, get_petsc_dir


import coffee.system
from coffee.plan import ASTKernel


class Kernel(base.Kernel):

    def _ast_to_c(self, ast, opts={}):
        """Transform an Abstract Syntax Tree representing the kernel into a
        string of code (C syntax) suitable to CPU execution."""
        ast_handler = ASTKernel(ast, self._include_dirs)
        ast_handler.plan_cpu(self._opts)
        return ast_handler.gencode()


class Arg(base.Arg):

    def c_arg_name(self, i=0, j=None):
        name = self.name
        if self._is_indirect and not (self._is_vec_map or self._uses_itspace):
            name = "%s_%d" % (name, self.idx)
        if i is not None:
            # For a mixed ParLoop we can't necessarily assume all arguments are
            # also mixed. If that's not the case we want index 0.
            if not self._is_mat and len(self.data) == 1:
                i = 0
            name += "_%d" % i
        if j is not None:
            name += "_%d" % j
        return name

    def c_vec_name(self):
        return self.c_arg_name() + "_vec"

    def c_map_name(self, i, j):
        return self.c_arg_name() + "_map%d_%d" % (i, j)

    def c_offset_name(self, i, j):
        return self.c_arg_name() + "_off%d_%d" % (i, j)

    def c_wrapper_arg(self):
        if self._is_mat:
            val = "Mat %s_" % self.c_arg_name()
        else:
            val = ', '.join(["%s *%s" % (self.ctype, self.c_arg_name(i))
                             for i in range(len(self.data))])
        if self._is_indirect or self._is_mat:
            for i, map in enumerate(as_tuple(self.map, Map)):
                if map is not None:
                    for j, m in enumerate(map):
                        val += ", %s *%s" % (as_cstr(IntType), self.c_map_name(i, j))
        return val

    def c_vec_dec(self, is_facet=False):
        facet_mult = 2 if is_facet else 1
        if self.map is not None:
            return "%(type)s *%(vec_name)s[%(arity)s];\n" % \
                {'type': self.ctype,
                 'vec_name': self.c_vec_name(),
                 'arity': self.map.arity * facet_mult}
        else:
            return "%(type)s *%(vec_name)s;\n" % \
                {'type': self.ctype,
                 'vec_name': self.c_vec_name()}

    def c_wrapper_dec(self):
        val = ""
        if self._is_mixed_mat:
            rows, cols = self.data.sparsity.shape
            for i in range(rows):
                for j in range(cols):
                    val += "Mat %(iname)s; MatNestGetSubMat(%(name)s_, %(i)d, %(j)d, &%(iname)s);\n" \
                        % {'name': self.c_arg_name(),
                           'iname': self.c_arg_name(i, j),
                           'i': i,
                           'j': j}
        elif self._is_mat:
            val += "Mat %(iname)s = %(name)s_;\n" % {'name': self.c_arg_name(),
                                                     'iname': self.c_arg_name(0, 0)}
        return val

    def c_ind_data(self, idx, i, j=0, is_top=False, offset=None, var=None):
        return "%(name)s + (%(map_name)s[%(var)s * %(arity)s + %(idx)s]%(top)s%(off_mul)s%(off_add)s)* %(dim)s%(off)s" % \
            {'name': self.c_arg_name(i),
             'map_name': self.c_map_name(i, 0),
             'var': var if var else 'i',
             'arity': self.map.split[i].arity,
             'idx': idx,
             'top': ' + start_layer' if is_top else '',
             'dim': self.data[i].cdim,
             'off': ' + %d' % j if j else '',
             'off_mul': ' * %d' % offset if is_top and offset is not None else '',
             'off_add': ' + %d' % offset if not is_top and offset is not None else ''}

    def c_ind_data_xtr(self, idx, i, j=0):
        return "%(name)s + (xtr_%(map_name)s[%(idx)s])*%(dim)s%(off)s" % \
            {'name': self.c_arg_name(i),
             'map_name': self.c_map_name(i, 0),
             'idx': idx,
             'dim': str(self.data[i].cdim),
             'off': ' + %d' % j if j else ''}

    def c_kernel_arg_name(self, i, j):
        return "p_%s" % self.c_arg_name(i, j)

    def c_global_reduction_name(self, count=None):
        return self.c_arg_name()

    def c_kernel_arg(self, count, i=0, j=0, shape=(0,), layers=1):
        if self._is_dat_view and not self._is_direct:
            raise NotImplementedError("Indirect DatView not implemented")
        if self._uses_itspace:
            if self._is_mat:
                if self.data[i, j]._is_vector_field:
                    return self.c_kernel_arg_name(i, j)
                elif self.data[i, j]._is_scalar_field:
                    return "(%(t)s (*)[%(dim)d])&%(name)s" % \
                        {'t': self.ctype,
                         'dim': shape[0],
                         'name': self.c_kernel_arg_name(i, j)}
                else:
                    raise RuntimeError("Don't know how to pass kernel arg %s" % self)
            else:
                if self.data is not None and self.data.dataset._extruded:
                    return self.c_ind_data_xtr("i_%d" % self.idx.index, i)
                else:
                    return self.c_ind_data("i_%d" % self.idx.index, i)
        elif self._is_indirect:
            if self._is_vec_map:
                return self.c_vec_name()
            return self.c_ind_data(self.idx, i)
        elif self._is_global_reduction:
            return self.c_global_reduction_name(count)
        elif isinstance(self.data, Global):
            return self.c_arg_name(i)
        else:
            if self._is_dat_view:
                idx = "(%(idx)s + i * %(dim)s)" % {'idx': self.data[i].index,
                                                   'dim': super(DatView, self.data[i]).cdim}
            else:
                idx = "(i * %(dim)s)" % {'dim': self.data[i].cdim}
            return "%(name)s + %(idx)s" % {'name': self.c_arg_name(i),
                                           'idx': idx}

    def c_vec_init(self, is_top, is_facet=False):
        is_top_init = is_top
        val = []
        vec_idx = 0
        for i, (m, d) in enumerate(zip(self.map, self.data)):
            is_top = is_top_init and m.iterset._extruded
            for idx in range(m.arity):
                val.append("%(vec_name)s[%(idx)s] = %(data)s" %
                           {'vec_name': self.c_vec_name(),
                            'idx': vec_idx,
                            'data': self.c_ind_data(idx, i, is_top=is_top,
                                                    offset=m.offset[idx] if is_top else None)})
                vec_idx += 1
            if is_facet:
                for idx in range(m.arity):
                    val.append("%(vec_name)s[%(idx)s] = %(data)s" %
                               {'vec_name': self.c_vec_name(),
                                'idx': vec_idx,
                                'data': self.c_ind_data(idx, i, is_top=is_top,
                                                        offset=m.offset[idx])})
                    vec_idx += 1
        return ";\n".join(val)

    def c_addto(self, i, j, buf_name, tmp_name, tmp_decl,
                extruded=None, is_facet=False):
        maps = as_tuple(self.map, Map)
        nrows = maps[0].split[i].arity
        ncols = maps[1].split[j].arity
        rows_str = "%s + i * %s" % (self.c_map_name(0, i), nrows)
        cols_str = "%s + i * %s" % (self.c_map_name(1, j), ncols)

        if extruded is not None:
            rows_str = extruded + self.c_map_name(0, i)
            cols_str = extruded + self.c_map_name(1, j)

        if is_facet:
            nrows *= 2
            ncols *= 2

        ret = []
        rbs, cbs = self.data.sparsity[i, j].dims[0][0]
        rdim = rbs * nrows
        addto_name = buf_name
        addto = 'MatSetValuesLocal'
        if self.data._is_vector_field:
            addto = 'MatSetValuesBlockedLocal'
            rmap, cmap = maps
            rdim, cdim = self.data.dims[i][j]
            if rmap.vector_index is not None or cmap.vector_index is not None:
                rows_str = "rowmap"
                cols_str = "colmap"
                addto = "MatSetValuesLocal"
                nbits = IntType.itemsize * 8 - 2
                fdict = {'nrows': nrows,
                         'ncols': ncols,
                         'rdim': rdim,
                         'cdim': cdim,
                         'rowmap': self.c_map_name(0, i),
                         'colmap': self.c_map_name(1, j),
                         'drop_full_row': 0 if rmap.vector_index is not None else 1,
                         'drop_full_col': 0 if cmap.vector_index is not None else 1,
                         'IntType': as_cstr(IntType),
                         'NBIT': nbits,
                         # UGH, need to make sure literals have
                         # correct type ("long int" if using 64 bit
                         # ints).
                         'ONE': {62: "1L", 30: "1"}[nbits],
                         'MASK': "0x%x%s" % (sum(2**(nbits - i) for i in range(3)),
                                             {62: "L", 30: ""}[nbits])}
                # Horrible hack alert
                # To apply BCs to a component of a Dat with cdim > 1
                # we encode which components to apply things to in the
                # high bits of the map value
                # The value that comes in is:
                # NBIT = (sizeof(IntType)*8 - 2)
                # -(row + 1 + sum_i 2 ** (NBIT - i))
                # where i are the components to zero
                #
                # So, the actual row (if it's negative) is:
                # MASK = sum_i 2**(NBIT - i)
                # (~input) & ~MASK
                # And we can determine which components to zero by
                # inspecting the high bits (1 << NBIT - i)
                ret.append("""
                %(IntType)s rowmap[%(nrows)d*%(rdim)d];
                %(IntType)s colmap[%(ncols)d*%(cdim)d];
                %(IntType)s block_row, block_col, tmp;
                int discard;
                for ( int j = 0; j < %(nrows)d; j++ ) {
                    block_row = %(rowmap)s[i*%(nrows)d + j];
                    discard = 0;
                    tmp = -(block_row + 1);
                    if ( block_row < 0 ) {
                        discard = 1;
                        block_row = tmp & ~%(MASK)s;
                    }
                    for ( int k = 0; k < %(rdim)d; k++ ) {
                        if ( discard && (!(tmp & %(MASK)s) || %(drop_full_row)d || ((tmp & (%(ONE)s << (%(NBIT)s - k))) != 0)) ) {
                            rowmap[j*%(rdim)d + k] = -1;
                        } else {
                            rowmap[j*%(rdim)d + k] = (block_row)*%(rdim)d + k;
                        }
                    }
                }
                for ( int j = 0; j < %(ncols)d; j++ ) {
                    discard = 0;
                    block_col = %(colmap)s[i*%(ncols)d + j];
                    tmp = -(block_col + 1);
                    if ( block_col < 0 ) {
                        discard = 1;
                        block_col = tmp & ~%(MASK)s;
                    }
                    for ( int k = 0; k < %(cdim)d; k++ ) {
                        if ( discard && (!(tmp & %(MASK)s) || %(drop_full_col)d || ((tmp & (%(ONE)s << (%(NBIT)s- k))) != 0)) ) {
                            colmap[j*%(cdim)d + k] = -1;
                        } else {
                            colmap[j*%(cdim)d + k] = (block_col)*%(cdim)d + k;
                        }
                    }
                }
                """ % fdict)
                nrows *= rdim
                ncols *= cdim
        ret.append("""%(addto)s(%(mat)s, %(nrows)s, %(rows)s,
                                         %(ncols)s, %(cols)s,
                                         (const PetscScalar *)%(vals)s,
                                         %(insert)s);""" %
                   {'mat': self.c_arg_name(i, j),
                    'vals': addto_name,
                    'addto': addto,
                    'nrows': nrows,
                    'ncols': ncols,
                    'rows': rows_str,
                    'cols': cols_str,
                    'IntType': as_cstr(IntType),
                    'insert': "INSERT_VALUES" if self.access == WRITE else "ADD_VALUES"})
        ret = " "*16 + "{\n" + "\n".join(ret) + "\n" + " "*16 + "}"
        return ret

    def c_add_offset(self, is_facet=False):
        if not self.map.iterset._extruded:
            return ""
        val = []
        vec_idx = 0
        for i, (m, d) in enumerate(zip(self.map, self.data)):
            for idx in range(m.arity):
                val.append("%(name)s[%(j)d] += %(offset)d * %(dim)s;" %
                           {'name': self.c_vec_name(),
                            'j': vec_idx,
                            'offset': m.offset[idx],
                            'dim': d.cdim})
                vec_idx += 1
            if is_facet:
                for idx in range(m.arity):
                    val.append("%(name)s[%(j)d] += %(offset)d * %(dim)s;" %
                               {'name': self.c_vec_name(),
                                'j': vec_idx,
                                'offset': m.offset[idx],
                                'dim': d.cdim})
                    vec_idx += 1
        return '\n'.join(val)+'\n'

    # New globals generation which avoids false sharing.
    def c_intermediate_globals_decl(self, count):
        return "%(type)s %(name)s_l%(count)s[1][%(dim)s]" % \
            {'type': self.ctype,
             'name': self.c_arg_name(),
             'count': str(count),
             'dim': self.data.cdim}

    def c_intermediate_globals_init(self, count):
        if self.access == INC:
            init = "(%(type)s)0" % {'type': self.ctype}
        else:
            init = "%(name)s[i]" % {'name': self.c_arg_name()}
        return "for ( int i = 0; i < %(dim)s; i++ ) %(name)s_l%(count)s[0][i] = %(init)s" % \
            {'dim': self.data.cdim,
             'name': self.c_arg_name(),
             'count': str(count),
             'init': init}

    def c_intermediate_globals_writeback(self, count):
        d = {'gbl': self.c_arg_name(),
             'local': "%(name)s_l%(count)s[0][i]" %
             {'name': self.c_arg_name(), 'count': str(count)}}
        if self.access == INC:
            combine = "%(gbl)s[i] += %(local)s" % d
        elif self.access == MIN:
            combine = "%(gbl)s[i] = %(gbl)s[i] < %(local)s ? %(gbl)s[i] : %(local)s" % d
        elif self.access == MAX:
            combine = "%(gbl)s[i] = %(gbl)s[i] > %(local)s ? %(gbl)s[i] : %(local)s" % d
        return """
#pragma omp critical
for ( int i = 0; i < %(dim)s; i++ ) %(combine)s;
""" % {'combine': combine, 'dim': self.data.cdim}

    def c_map_decl(self, is_facet=False):
        if self._is_mat:
            dsets = self.data.sparsity.dsets
        else:
            dsets = (self.data.dataset,)
        val = []
        for i, (map, dset) in enumerate(zip(as_tuple(self.map, Map), dsets)):
            for j, (m, d) in enumerate(zip(map, dset)):
                dim = m.arity
                if is_facet:
                    dim *= 2
                val.append("%(IntType)s xtr_%(name)s[%(dim)s];" %
                           {'name': self.c_map_name(i, j),
                            'dim': dim,
                            'IntType': as_cstr(IntType)})
        return '\n'.join(val)+'\n'

    def c_map_init(self, is_top=False, is_facet=False):
        if self._is_mat:
            dsets = self.data.sparsity.dsets
        else:
            dsets = (self.data.dataset,)
        val = []
        for i, (map, dset) in enumerate(zip(as_tuple(self.map, Map), dsets)):
            for j, (m, d) in enumerate(zip(map, dset)):
                for idx in range(m.arity):
                    val.append("xtr_%(name)s[%(ind)s] = *(%(name)s + i * %(dim)s + %(ind)s)%(off_top)s;" %
                               {'name': self.c_map_name(i, j),
                                'dim': m.arity,
                                'ind': idx,
                                'off_top': ' + start_layer * '+str(m.offset[idx]) if is_top else ''})
                if is_facet:
                    for idx in range(m.arity):
                        val.append("xtr_%(name)s[%(ind)s] = *(%(name)s + i * %(dim)s + %(ind_zero)s)%(off_top)s%(off)s;" %
                                   {'name': self.c_map_name(i, j),
                                    'dim': m.arity,
                                    'ind': idx + m.arity,
                                    'ind_zero': idx,
                                    'off_top': ' + start_layer' if is_top else '',
                                    'off': ' + ' + str(m.offset[idx])})
        return '\n'.join(val)+'\n'

    def c_map_bcs(self, sign, is_facet):
        maps = as_tuple(self.map, Map)
        val = []
        # To throw away boundary condition values, we subtract a large
        # value from the map to make it negative then add it on later to
        # get back to the original
        max_int = 10000000

        need_bottom = False
        # Apply any bcs on the first (bottom) layer
        for i, map in enumerate(maps):
            if not map.iterset._extruded:
                continue
            for j, m in enumerate(map):
                bottom_masks = None
                for location, name in m.implicit_bcs:
                    if location == "bottom":
                        if bottom_masks is None:
                            bottom_masks = m.bottom_mask[name].copy()
                        else:
                            bottom_masks += m.bottom_mask[name]
                        need_bottom = True
                if bottom_masks is not None:
                    for idx in range(m.arity):
                        if bottom_masks[idx] < 0:
                            val.append("xtr_%(name)s[%(ind)s] %(sign)s= %(val)s;" %
                                       {'name': self.c_map_name(i, j),
                                        'val': max_int,
                                        'ind': idx,
                                        'sign': sign})
        if need_bottom:
            val.insert(0, "if (j_0 == 0) {")
            val.append("}")

        need_top = False
        pos = len(val)
        # Apply any bcs on last (top) layer
        for i, map in enumerate(maps):
            if not map.iterset._extruded:
                continue
            for j, m in enumerate(map):
                top_masks = None
                for location, name in m.implicit_bcs:
                    if location == "top":
                        if top_masks is None:
                            top_masks = m.top_mask[name].copy()
                        else:
                            top_masks += m.top_mask[name]
                        need_top = True
                if top_masks is not None:
                    facet_offset = m.arity if is_facet else 0
                    for idx in range(m.arity):
                        if top_masks[idx] < 0:
                            val.append("xtr_%(name)s[%(ind)s] %(sign)s= %(val)s;" %
                                       {'name': self.c_map_name(i, j),
                                        'val': max_int,
                                        'ind': idx + facet_offset,
                                        'sign': sign})
        if need_top:
            val.insert(pos, "if (j_0 == top_layer - 1) {")
            val.append("}")
        return '\n'.join(val)+'\n'

    def c_add_offset_map(self, is_facet=False):
        if self._is_mat:
            dsets = self.data.sparsity.dsets
        else:
            dsets = (self.data.dataset,)
        val = []
        for i, (map, dset) in enumerate(zip(as_tuple(self.map, Map), dsets)):
            if not map.iterset._extruded:
                continue
            for j, (m, d) in enumerate(zip(map, dset)):
                for idx in range(m.arity):
                    val.append("xtr_%(name)s[%(ind)s] += %(off)d;" %
                               {'name': self.c_map_name(i, j),
                                'off': m.offset[idx],
                                'ind': idx})
                if is_facet:
                    for idx in range(m.arity):
                        val.append("xtr_%(name)s[%(ind)s] += %(off)d;" %
                                   {'name': self.c_map_name(i, j),
                                    'off': m.offset[idx],
                                    'ind': m.arity + idx})
        return '\n'.join(val)+'\n'

    def c_buffer_decl(self, size, idx, buf_name, is_facet=False, init=True):
        buf_type = self.data.ctype
        dim = len(size)
        compiler = coffee.system.compiler
        isa = coffee.system.isa
        align = compiler['align'](isa["alignment"]) if compiler and size[-1] % isa["dp_reg"] == 0 else ""
        init_expr = " = " + "{" * dim + "0.0" + "}" * dim if self.access in [WRITE, INC] else ""
        if not init:
            init_expr = ""

        return "%(typ)s %(name)s%(dim)s%(align)s%(init)s" % \
            {"typ": buf_type,
             "name": buf_name,
             "dim": "".join(["[%d]" % (d * (2 if is_facet else 1)) for d in size]),
             "align": " " + align,
             "init": init_expr}

    def c_buffer_gather(self, size, idx, buf_name):
        dim = self.data.cdim
        return ";\n".join(["%(name)s[i_0*%(dim)d%(ofs)s] = *(%(ind)s%(ofs)s);\n" %
                           {"name": buf_name,
                            "dim": dim,
                            "ind": self.c_kernel_arg(idx),
                            "ofs": " + %s" % j if j else ""} for j in range(dim)])

    def c_buffer_scatter_vec(self, count, i, j, mxofs, buf_name):
        dim = self.data.split[i].cdim
        return ";\n".join(["*(%(ind)s%(nfofs)s) %(op)s %(name)s[i_0*%(dim)d%(nfofs)s%(mxofs)s]" %
                           {"ind": self.c_kernel_arg(count, i, j),
                            "op": "=" if self.access == WRITE else "+=",
                            "name": buf_name,
                            "dim": dim,
                            "nfofs": " + %d" % o if o else "",
                            "mxofs": " + %d" % (mxofs[0] * dim) if mxofs else ""}
                           for o in range(dim)])


class JITModule(base.JITModule):

    _wrapper = """
void %(wrapper_name)s(int start,
                      int end,
                      %(ssinds_arg)s
                      %(wrapper_args)s
                      %(layer_arg)s) {
  %(user_code)s
  %(wrapper_decs)s;
  %(map_decl)s
  %(vec_decs)s;
  for ( int n = start; n < end; n++ ) {
    %(IntType)s i = %(index_expr)s;
    %(vec_inits)s;
    %(map_init)s;
    %(extr_loop)s
    %(map_bcs_m)s;
    %(buffer_decl)s;
    %(buffer_gather)s
    %(kernel_name)s(%(kernel_args)s);
    %(itset_loop_body)s
    %(map_bcs_p)s;
    %(apply_offset)s;
    %(extr_loop_close)s
  }
}
"""

    _cppargs = []
    _libraries = []
    _system_headers = []
    _extension = 'c'

    def __init__(self, kernel, itspace, *args, **kwargs):
        """
        A cached compiled function to execute for a specified par_loop.

        See :func:`~.par_loop` for the description of arguments.

        .. warning ::

           Note to implementors.  This object is *cached*, and therefore
           should not hold any long term references to objects that
           you want to be collected.  In particular, after the
           ``args`` have been inspected to produce the compiled code,
           they **must not** remain part of the object's slots,
           otherwise they (and the :class:`~.Dat`\s, :class:`~.Map`\s
           and :class:`~.Mat`\s they reference) will never be collected.
        """
        # Return early if we were in the cache.
        if self._initialized:
            return
        self.comm = itspace.comm
        self._kernel = kernel
        self._fun = None
        self._code_dict = None
        self._itspace = itspace
        self._args = args
        self._direct = kwargs.get('direct', False)
        self._iteration_region = kwargs.get('iterate', ALL)
        self._pass_layer_arg = kwargs.get('pass_layer_arg', False)
        # Copy the class variables, so we don't overwrite them
        self._cppargs = dcopy(type(self)._cppargs)
        self._libraries = dcopy(type(self)._libraries)
        self._system_headers = dcopy(type(self)._system_headers)
        self.set_argtypes(itspace.iterset, *args)
        if not kwargs.get('delay', False):
            self.compile()
            self._initialized = True

    @collective
    def __call__(self, *args):
        return self._fun(*args)

    @property
    def _wrapper_name(self):
        return 'wrap_%s' % self._kernel.name

    @collective
    def compile(self):
        # If we weren't in the cache we /must/ have arguments
        if not hasattr(self, '_args'):
            raise RuntimeError("JITModule has no args associated with it, should never happen")

        compiler = coffee.system.compiler
        externc_open = '' if not self._kernel._cpp else 'extern "C" {'
        externc_close = '' if not self._kernel._cpp else '}'
        headers = "\n".join([compiler.get('vect_header', "")])
        if any(arg._is_soa for arg in self._args):
            kernel_code = """
            #define OP2_STRIDE(a, idx) a[idx]
            %(header)s
            %(code)s
            #undef OP2_STRIDE
            """ % {'code': self._kernel.code(),
                   'header': headers}
        else:
            kernel_code = """
            %(header)s
            %(code)s
            """ % {'code': self._kernel.code(),
                   'header': headers}
        code_to_compile = strip(dedent(self._wrapper) % self.generate_code())

        code_to_compile = """
        #include <petsc.h>
        #include <stdbool.h>
        #include <math.h>
        #include <inttypes.h>
        %(sys_headers)s

        %(kernel)s

        %(externc_open)s
        %(wrapper)s
        %(externc_close)s
        """ % {'kernel': kernel_code,
               'wrapper': code_to_compile,
               'externc_open': externc_open,
               'externc_close': externc_close,
               'sys_headers': '\n'.join(self._kernel._headers + self._system_headers)}

        self._dump_generated_code(code_to_compile)
        if configuration["debug"]:
            self._wrapper_code = code_to_compile

        extension = self._extension
        cppargs = self._cppargs
        cppargs += ["-I%s/include" % d for d in get_petsc_dir()] + \
                   ["-I%s" % d for d in self._kernel._include_dirs] + \
                   ["-I%s" % os.path.abspath(os.path.dirname(__file__))]
        if compiler:
            cppargs += [compiler[coffee.system.isa['inst_set']]]
        ldargs = ["-L%s/lib" % d for d in get_petsc_dir()] + \
                 ["-Wl,-rpath,%s/lib" % d for d in get_petsc_dir()] + \
                 ["-lpetsc", "-lm"] + self._libraries
        ldargs += self._kernel._ldargs

        if self._kernel._cpp:
            extension = "cpp"
        self._fun = compilation.load(code_to_compile,
                                     extension,
                                     self._wrapper_name,
                                     cppargs=cppargs,
                                     ldargs=ldargs,
                                     argtypes=self._argtypes,
                                     restype=None,
                                     compiler=compiler.get('name'),
                                     comm=self.comm)
        # Blow away everything we don't need any more
        del self._args
        del self._kernel
        del self._itspace
        del self._direct
        return self._fun

    def generate_code(self):
        if not self._code_dict:
            self._code_dict = wrapper_snippets(self._itspace, self._args,
                                               kernel_name=self._kernel._name,
                                               user_code=self._kernel._user_code,
                                               wrapper_name=self._wrapper_name,
                                               iteration_region=self._iteration_region,
                                               pass_layer_arg=self._pass_layer_arg)
        return self._code_dict

    def set_argtypes(self, iterset, *args):
        index_type = as_ctypes(IntType)
        argtypes = [index_type, index_type]
        if isinstance(iterset, Subset):
            argtypes.append(iterset._argtype)
        for arg in args:
            if arg._is_mat:
                argtypes.append(arg.data._argtype)
            else:
                for d in arg.data:
                    argtypes.append(d._argtype)
            if arg._is_indirect or arg._is_mat:
                maps = as_tuple(arg.map, Map)
                for map in maps:
                    if map is not None:
                        for m in map:
                            argtypes.append(m._argtype)

        if iterset._extruded:
            argtypes.append(index_type)
            argtypes.append(index_type)

        self._argtypes = argtypes


class ParLoop(petsc_base.ParLoop):

    def prepare_arglist(self, iterset, *args):
        arglist = []
        if isinstance(iterset, Subset):
            arglist.append(iterset._indices.ctypes.data)

        for arg in args:
            if arg._is_mat:
                arglist.append(arg.data.handle.handle)
            else:
                for d in arg.data:
                    # Cannot access a property of the Dat or we will force
                    # evaluation of the trace
                    arglist.append(d._data.ctypes.data)
            if arg._is_indirect or arg._is_mat:
                for map in arg._map:
                    if map is not None:
                        for m in map:
                            arglist.append(m._values.ctypes.data)

        if iterset._extruded:
            region = self.iteration_region
            # Set up appropriate layer iteration bounds
            if region is ON_BOTTOM:
                arglist.append(0)
                arglist.append(1)
                arglist.append(iterset.layers - 1)
            elif region is ON_TOP:
                arglist.append(iterset.layers - 2)
                arglist.append(iterset.layers - 1)
                arglist.append(iterset.layers - 1)
            elif region is ON_INTERIOR_FACETS:
                arglist.append(0)
                arglist.append(iterset.layers - 2)
                arglist.append(iterset.layers - 2)
            else:
                arglist.append(0)
                arglist.append(iterset.layers - 1)
                arglist.append(iterset.layers - 1)
        return arglist

    @cached_property
    def _jitmodule(self):
        return JITModule(self.kernel, self.it_space, *self.args,
                         direct=self.is_direct, iterate=self.iteration_region,
                         pass_layer_arg=self._pass_layer_arg)

    @collective
    def _compute(self, part, fun, *arglist):
        with timed_region("ParLoop%s" % self.iterset.name):
            fun(part.offset, part.offset + part.size, *arglist)
            self.log_flops()


def wrapper_snippets(itspace, args,
                     kernel_name=None, wrapper_name=None, user_code=None,
                     iteration_region=ALL, pass_layer_arg=False):
    """Generates code snippets for the wrapper,
    ready to be into a template.

    :param itspace: :class:`IterationSpace` object of the :class:`ParLoop`,
                    This is built from the iteration :class:`Set`.
    :param args: :class:`Arg`s of the :class:`ParLoop`
    :param kernel_name: Kernel function name (forwarded)
    :param user_code: Code to insert into the wrapper (forwarded)
    :param wrapper_name: Wrapper function name (forwarded)
    :param iteration_region: Iteration region, this is specified when
                             creating a :class:`ParLoop`.

    :return: dict containing the code snippets
    """

    assert kernel_name is not None
    if wrapper_name is None:
        wrapper_name = "wrap_" + kernel_name
    if user_code is None:
        user_code = ""

    direct = all(a.map is None for a in args)

    def itspace_loop(i, d):
        return "for (int i_%d=0; i_%d<%d; ++i_%d) {" % (i, i, d, i)

    def extrusion_loop():
        if direct:
            return "{"
        return "for (int j_0 = start_layer; j_0 < end_layer; ++j_0){"

    _ssinds_arg = ""
    _index_expr = "(%s)n" % as_cstr(IntType)
    is_top = (iteration_region == ON_TOP)
    is_facet = (iteration_region == ON_INTERIOR_FACETS)

    if isinstance(itspace._iterset, Subset):
        _ssinds_arg = "%s* ssinds," % as_cstr(IntType)
        _index_expr = "ssinds[n]"

    _wrapper_args = ', '.join([arg.c_wrapper_arg() for arg in args])

    # Pass in the is_facet flag to mark the case when it's an interior horizontal facet in
    # an extruded mesh.
    _wrapper_decs = ';\n'.join([arg.c_wrapper_dec() for arg in args])

    _vec_decs = ';\n'.join([arg.c_vec_dec(is_facet=is_facet) for arg in args if arg._is_vec_map])

    _intermediate_globals_decl = ';\n'.join(
        [arg.c_intermediate_globals_decl(count)
         for count, arg in enumerate(args)
         if arg._is_global_reduction])
    _intermediate_globals_init = ';\n'.join(
        [arg.c_intermediate_globals_init(count)
         for count, arg in enumerate(args)
         if arg._is_global_reduction])
    _intermediate_globals_writeback = ';\n'.join(
        [arg.c_intermediate_globals_writeback(count)
         for count, arg in enumerate(args)
         if arg._is_global_reduction])

    _vec_inits = ';\n'.join([arg.c_vec_init(is_top, is_facet=is_facet) for arg in args
                             if not arg._is_mat and arg._is_vec_map])

    indent = lambda t, i: ('\n' + '  ' * i).join(t.split('\n'))

    _map_decl = ""
    _apply_offset = ""
    _map_init = ""
    _extr_loop = ""
    _extr_loop_close = ""
    _map_bcs_m = ""
    _map_bcs_p = ""
    _layer_arg = ""
    if itspace._extruded:
        _layer_arg = ", int start_layer, int end_layer, int top_layer"
        _map_decl += ';\n'.join([arg.c_map_decl(is_facet=is_facet)
                                 for arg in args if arg._uses_itspace])
        _map_init += ';\n'.join([arg.c_map_init(is_top=is_top, is_facet=is_facet)
                                 for arg in args if arg._uses_itspace])
        _map_bcs_m += ';\n'.join([arg.c_map_bcs("-", is_facet) for arg in args if arg._is_mat])
        _map_bcs_p += ';\n'.join([arg.c_map_bcs("+", is_facet) for arg in args if arg._is_mat])
        _apply_offset += ';\n'.join([arg.c_add_offset_map(is_facet=is_facet)
                                     for arg in args if arg._uses_itspace])
        _apply_offset += ';\n'.join([arg.c_add_offset(is_facet=is_facet)
                                     for arg in args if arg._is_vec_map])
        _extr_loop = '\n' + extrusion_loop()
        _extr_loop_close = '}\n'

    # Build kernel invocation. Let X be a parameter of the kernel representing a
    # tensor accessed in an iteration space. Let BUFFER be an array of the same
    # size as X.  BUFFER is declared and intialized in the wrapper function.
    # In particular, if:
    # - X is written or incremented, then BUFFER is initialized to 0
    # - X is read, then BUFFER gathers data expected by X
    _buf_name, _tmp_decl, _tmp_name = {}, {}, {}
    _buf_decl, _buf_gather = OrderedDict(), OrderedDict()  # Deterministic code generation
    for count, arg in enumerate(args):
        if not arg._uses_itspace:
            continue
        _buf_name[arg] = "buffer_%s" % arg.c_arg_name(count)
        _tmp_name[arg] = "tmp_%s" % _buf_name[arg]
        _buf_size = list(itspace._extents)
        if not arg._is_mat:
            # Readjust size to take into account the size of a vector space
            _dat_size = (arg.data.cdim,)
            _buf_size = [sum([e*d for e, d in zip(_buf_size, _dat_size)])]
            _loop_size = [_buf_size[i]//_dat_size[i] for i in range(len(_buf_size))]
        else:
            _dat_size = arg.data.dims[0][0]  # TODO: [0][0] ?
            _buf_size = [e*d for e, d in zip(_buf_size, _dat_size)]
        _buf_decl[arg] = arg.c_buffer_decl(_buf_size, count, _buf_name[arg], is_facet=is_facet)
        _tmp_decl[arg] = arg.c_buffer_decl(_buf_size, count, _tmp_name[arg], is_facet=is_facet,
                                           init=False)
        if arg.access not in [WRITE, INC]:
            _itspace_loops = '\n'.join(['  ' * n + itspace_loop(n, e) for n, e in enumerate(_loop_size)])
            _buf_gather[arg] = arg.c_buffer_gather(_buf_size, count, _buf_name[arg])
            _itspace_loop_close = '\n'.join('  ' * n + '}' for n in range(len(_loop_size) - 1, -1, -1))
            _buf_gather[arg] = "\n".join([_itspace_loops, _buf_gather[arg], _itspace_loop_close])
    _kernel_args = ', '.join([arg.c_kernel_arg(count) if not arg._uses_itspace else _buf_name[arg]
                              for count, arg in enumerate(args)])

    if pass_layer_arg:
        _kernel_args += ", j_0"

    _buf_gather = ";\n".join(_buf_gather.values())
    _buf_decl = ";\n".join(_buf_decl.values())

    def itset_loop_body(i, j, shape, offsets, is_facet=False):
        template_scatter = """
    %(offset_decl)s;
    %(ofs_itspace_loops)s
    %(ind)s%(offset)s
    %(ofs_itspace_loop_close)s
    %(itspace_loops)s
    %(ind)s%(buffer_scatter)s;
    %(itspace_loop_close)s
"""
        nloops = len(shape)
        mult = 1 if not is_facet else 2
        _buf_scatter = OrderedDict()  # Deterministic code generation
        for count, arg in enumerate(args):
            if not (arg._uses_itspace and arg.access in [WRITE, INC]):
                continue
            elif (arg._is_mat and arg._is_mixed) or (arg._is_dat and nloops > 1):
                raise NotImplementedError
            elif arg._is_mat:
                continue
            elif arg._is_dat:
                loop_size = shape[0]*mult
                _itspace_loops, _itspace_loop_close = itspace_loop(0, loop_size), '}'
                _scatter_stmts = arg.c_buffer_scatter_vec(count, i, j, offsets, _buf_name[arg])
                _buf_offset, _buf_offset_decl = '', ''
            else:
                raise NotImplementedError
            _buf_scatter[arg] = template_scatter % {
                'ind': '  ' * nloops,
                'offset_decl': _buf_offset_decl,
                'offset': _buf_offset,
                'buffer_scatter': _scatter_stmts,
                'itspace_loops': indent(_itspace_loops, 2),
                'itspace_loop_close': indent(_itspace_loop_close, 2),
                'ofs_itspace_loops': indent(_itspace_loops, 2) if _buf_offset else '',
                'ofs_itspace_loop_close': indent(_itspace_loop_close, 2) if _buf_offset else ''
            }
        scatter = ";\n".join(_buf_scatter.values())

        if itspace._extruded:
            _addtos_extruded = ';\n'.join([arg.c_addto(i, j, _buf_name[arg],
                                                       _tmp_name[arg],
                                                       _tmp_decl[arg],
                                                       "xtr_", is_facet=is_facet)
                                           for arg in args if arg._is_mat])
            _addtos = ""
        else:
            _addtos_extruded = ""
            _addtos = ';\n'.join([arg.c_addto(i, j, _buf_name[arg],
                                              _tmp_name[arg],
                                              _tmp_decl[arg])
                                  for count, arg in enumerate(args) if arg._is_mat])

        if not _buf_scatter:
            _itspace_loops = ''
            _itspace_loop_close = ''

        template = """
    %(scatter)s
    %(ind)s%(addtos_extruded)s;
    %(addtos)s;
"""
        return template % {
            'ind': '  ' * nloops,
            'scatter': scatter,
            'addtos_extruded': indent(_addtos_extruded, 2 + nloops),
            'addtos': indent(_addtos, 2),
        }

    return {'kernel_name': kernel_name,
            'wrapper_name': wrapper_name,
            'ssinds_arg': _ssinds_arg,
            'index_expr': _index_expr,
            'wrapper_args': _wrapper_args,
            'user_code': user_code,
            'wrapper_decs': indent(_wrapper_decs, 1),
            'vec_inits': indent(_vec_inits, 2),
            'layer_arg': _layer_arg,
            'map_decl': indent(_map_decl, 2),
            'vec_decs': indent(_vec_decs, 2),
            'map_init': indent(_map_init, 5),
            'apply_offset': indent(_apply_offset, 3),
            'extr_loop': indent(_extr_loop, 5),
            'map_bcs_m': indent(_map_bcs_m, 5),
            'map_bcs_p': indent(_map_bcs_p, 5),
            'extr_loop_close': indent(_extr_loop_close, 2),
            'interm_globals_decl': indent(_intermediate_globals_decl, 3),
            'interm_globals_init': indent(_intermediate_globals_init, 3),
            'interm_globals_writeback': indent(_intermediate_globals_writeback, 3),
            'buffer_decl': _buf_decl,
            'buffer_gather': _buf_gather,
            'kernel_args': _kernel_args,
            'IntType': as_cstr(IntType),
            'itset_loop_body': '\n'.join([itset_loop_body(i, j, shape, offsets, is_facet=(iteration_region == ON_INTERIOR_FACETS))
                                          for i, j, shape, offsets in itspace])}


def generate_cell_wrapper(itspace, args, forward_args=(), kernel_name=None, wrapper_name=None):
    """Generates wrapper for a single cell. No iteration loop, but cellwise data is extracted.
    Cell is expected as an argument to the wrapper. For extruded, the numbering of the cells
    is columnwise continuous, bottom to top.

    :param itspace: :class:`IterationSpace` object. Can be built from
                    iteration :class:`Set` using pyop2.base.build_itspace
    :param args: :class:`Arg`s
    :param forward_args: To forward unprocessed arguments to the kernel via the wrapper,
                         give an iterable of strings describing their C types.
    :param kernel_name: Kernel function name
    :param wrapper_name: Wrapper function name

    :return: string containing the C code for the single-cell wrapper
    """

    direct = all(a.map is None for a in args)
    snippets = wrapper_snippets(itspace, args, kernel_name=kernel_name, wrapper_name=wrapper_name)

    if itspace._extruded:
        snippets['index_exprs'] = """{0} i = cell / nlayers;
    {0} j = cell % nlayers;""".format(as_cstr(IntType))
        snippets['nlayers_arg'] = ", {0} nlayers".format(as_cstr(IntType))
        snippets['extr_pos_loop'] = "{" if direct else "for ({0} j_0 = 0; j_0 < j; ++j_0) {{".format(as_cstr(IntType))
    else:
        snippets['index_exprs'] = "{0} i = cell;".format(as_cstr(IntType))
        snippets['nlayers_arg'] = ""
        snippets['extr_pos_loop'] = ""

    snippets['wrapper_fargs'] = "".join("{1} farg{0}, ".format(i, arg) for i, arg in enumerate(forward_args))
    snippets['kernel_fargs'] = "".join("farg{0}, ".format(i) for i in range(len(forward_args)))

    snippets['IntType'] = as_cstr(IntType)
    template = """
#include <inttypes.h>

static inline void %(wrapper_name)s(%(wrapper_fargs)s%(wrapper_args)s%(nlayers_arg)s, %(IntType)s cell)
{
    %(user_code)s
    %(wrapper_decs)s;
    %(map_decl)s
    %(vec_decs)s;
    %(index_exprs)s
    %(vec_inits)s;
    %(map_init)s;
    %(extr_pos_loop)s
        %(apply_offset)s;
    %(extr_loop_close)s
    %(map_bcs_m)s;
    %(buffer_decl)s;
    %(buffer_gather)s
    %(kernel_name)s(%(kernel_fargs)s%(kernel_args)s);
    %(itset_loop_body)s
    %(map_bcs_p)s;
}
"""
    return template % snippets
