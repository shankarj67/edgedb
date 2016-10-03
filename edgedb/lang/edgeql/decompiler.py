##
# Copyright (c) 2008-2015 MagicStack Inc.
# All rights reserved.
#
# See LICENSE for details.
##


import collections

from edgedb.lang.ir import ast as irast
from edgedb.lang.edgeql import ast as qlast

from edgedb.lang.common import ast


class IRDecompilerContext:
    pass


class IRDecompiler:
    def transform(self, edgedb_tree, inline_anchors=False, return_statement=False):
        context = IRDecompilerContext()
        context.inline_anchors = inline_anchors
        edgeql_tree = self._process_expr(context, edgedb_tree)

        if return_statement:
            if not isinstance(edgeql_tree, qlast.StatementNode):
                selnode = qlast.SelectQueryNode()
                selnode.targets = [qlast.SelectExprNode(expr=edgeql_tree)]
                edgeql_tree = selnode

        return edgeql_tree

    def _pathspec_from_record(self, context, expr):
        pathspec = []

        ptr_iters = set()

        for el in expr.elements:
            if el.rewrite_original:
                el = el.rewrite_original

            if isinstance(el, irast.MetaRef):
                continue

            elif isinstance(el, (irast.AtomicRefSimple, irast.LinkPropRefSimple)):
                rlink = el.rlink if isinstance(el, irast.AtomicRefSimple) else el.ref

                trigger = rlink.pathspec_trigger
                if trigger is None:
                    continue

                if isinstance(trigger, irast.PointerIteratorPathSpecTrigger):
                    ptr_iters.add(frozenset(trigger.filters.items()))
                elif isinstance(trigger, irast.ExplicitPathSpecTrigger):
                    refpath = self._process_expr(context, el)
                    sitem = qlast.SelectPathSpecNode(expr=refpath.steps[-1])
                else:
                    msg = 'unexpected pathspec trigger in record ref: {!r}'.format(trigger)
                    raise ValueError(msg)

            elif isinstance(el, irast.SubgraphRef):
                rlink = el.rlink

                trigger = rlink.pathspec_trigger
                if trigger is None:
                    continue
                elif isinstance(trigger, irast.PointerIteratorPathSpecTrigger):
                    ptr_iters.add(frozenset(trigger.filters.items()))
                    continue
                elif not isinstance(trigger, irast.ExplicitPathSpecTrigger):
                    msg = 'unexpected pathspec trigger in record ref: {!r}'.format(trigger)
                    raise ValueError(msg)

                target = el.rlink.target.concept.name
                target = qlast.ClassRefNode(name=target.name, module=target.module)

                refpath = qlast.LinkNode(name=el.rlink.link_class.normal_name().name,
                                         namespace=el.rlink.link_class.normal_name().module,
                                         target=target, direction=el.rlink.direction)
                refpath = qlast.LinkExprNode(expr=refpath)
                sitem = qlast.SelectPathSpecNode(expr=refpath)
                rec = el.ref.selector[0].expr

                if isinstance(rec, irast.Record):
                    sitem.pathspec = self._pathspec_from_record(context, rec)

                elif isinstance(rec, irast.LinkPropRefSimple):
                    proprefpath = self._process_expr(context, rec)
                    sitem = qlast.SelectPathSpecNode(expr=proprefpath.steps[-1])

                else:
                    raise ValueError('unexpected node in subgraph ref: {!r}'.format(rec))

            elif isinstance(el, irast.Record):
                rlink = el.rlink

                trigger = rlink.pathspec_trigger

                if trigger is None:
                    continue
                elif isinstance(trigger, irast.PointerIteratorPathSpecTrigger):
                    ptr_iters.add(frozenset(trigger.filters.items()))
                    continue
                elif not isinstance(trigger, irast.ExplicitPathSpecTrigger):
                    msg = 'unexpected pathspec trigger in record ref: {!r}'.format(trigger)
                    raise ValueError(msg)

                target = el.concept.name
                target = qlast.ClassRefNode(name=target.name, module=target.module)

                refpath = qlast.LinkNode(name=el.rlink.link_class.normal_name().name,
                                         namespace=el.rlink.link_class.normal_name().module,
                                         target=target, direction=el.rlink.direction)
                refpath = qlast.LinkExprNode(expr=refpath)
                sitem = qlast.SelectPathSpecNode(expr=refpath)
                sitem.pathspec = self._pathspec_from_record(context, el)

            else:
                raise ValueError('unexpected node in record: {!r}'.format(el))

            pathspec.append(sitem)

        for ptr_iter in ptr_iters:
            filters = []
            for prop, val in ptr_iter:
                flt = qlast.PointerGlobFilter(property=prop, value=val, any=val is None)
                filters.append(flt)

            pathspec.append(qlast.PointerGlobNode(filters=filters))

        return pathspec

    def _process_path_combination_as_filter(self, context, expr):
        if isinstance(expr, irast.Disjunction):
            op = ast.ops.OR
        else:
            op = ast.ops.AND

        operands = []

        for elem in expr.paths:
            if isinstance(elem, irast.PathCombination):
                elem = self._process_path_combination_as_filter(context, elem)
            elif not isinstance(elem, irast.Path):
                elem = self._process_expr(context, elem)
            else:
                elem = None

            if elem is not None:
                operands.append(elem)

        if len(operands) == 0:
            return None
        elif len(operands) == 1:
            return operands[0]
        else:
            result = qlast.BinOpNode(left=operands[0], right=operands[1], op=op)

            for operand in operands[2:]:
                result = qlast.BinOpNode(left=result, right=operand, op=op)

            return result

    def _is_none(self, context, expr):
        return (isinstance(expr, (irast.Constant, qlast.ConstantNode))
                and expr.value is None and expr.index is None)

    def _process_function(self, context, expr):
        args = [self._process_expr(context, arg) for arg in expr.args]
        result = qlast.FunctionCallNode(func=expr.name, args=args)

        return result

    def _process_expr(self, context, expr):
        if expr.rewrite_original:
            # Process original node instead of a rewrite
            return self._process_expr(context, expr.rewrite_original)
        elif expr.is_rewrite_product:
            # Skip all rewrite products
            return None

        if isinstance(expr, irast.GraphExpr):
            result = qlast.SelectQueryNode()

            if expr.generator:
                if not isinstance(expr.generator, irast.Path):
                    result.where = self._process_expr(context, expr.generator)
                elif isinstance(expr.generator, irast.PathCombination):
                    result.where = self._process_path_combination_as_filter(context, expr.generator)
            else:
                result.where = None
            result.groupby = [self._process_expr(context, e) for e in expr.grouper]
            result.orderby = [self._process_expr(context, e) for e in expr.sorter]
            result.targets = [self._process_expr(context, e) for e in expr.selector]

            if expr.limit is not None:
                result.limit = self._process_expr(context, expr.limit)

            if expr.offset is not None:
                result.offset = self._process_expr(context, expr.offset)

        elif isinstance(expr, irast.InlineFilter):
            result = self._process_expr(context, expr.expr)

        elif isinstance(expr, irast.InlinePropFilter):
            result = self._process_expr(context, expr.expr)

        elif isinstance(expr, irast.Constant):
            if expr.expr is not None:
                result = self._process_expr(context, expr.expr)
            else:
                value = expr.value
                index = expr.index

                if  (isinstance(value, collections.Container)
                                and not isinstance(value, (str, bytes))):

                    elements = []

                    for v in value:
                        if isinstance(v, edgedb_types.Class):
                            v = qlast.ClassRefNode(module=v.name.module, name=v.name.name)
                        elements.append(v)

                    result = qlast.SequenceNode(elements=elements)
                else:
                    result = qlast.ConstantNode(value=value, index=index)

        elif isinstance(expr, irast.SelectorExpr):
            result = qlast.SelectExprNode(expr=self._process_expr(context, expr.expr))

        elif isinstance(expr, irast.SortExpr):
            result = qlast.SortExprNode(path=self._process_expr(context, expr.expr),
                                        direction=expr.direction,
                                        nones_order=expr.nones_order)

        elif isinstance(expr, irast.FunctionCall):
            result = self._process_function(context, expr)

        elif isinstance(expr, irast.Record):
            if expr.rlink:
                result = self._process_expr(context, expr.rlink)
            else:
                path = qlast.PathNode()
                step = qlast.PathStepNode(expr=expr.concept.name.name,
                                          namespace=expr.concept.name.module)
                path.steps.append(step)
                path.pathspec = self._pathspec_from_record(context, expr)
                result = path

        elif isinstance(expr, irast.UnaryOp):
            operand = self._process_expr(context, expr.expr)
            result = qlast.UnaryOpNode(op=expr.op, operand=operand)

        elif isinstance(expr, irast.BinOp):
            left = self._process_expr(context, expr.left)
            right = self._process_expr(context, expr.right)

            if left is not None and right is not None:
                result = qlast.BinOpNode(left=left, op=expr.op, right=right)
            else:
                result = left or right

        elif isinstance(expr, irast.ExistPred):
            result = qlast.ExistsPredicateNode(expr=self._process_expr(context, expr.expr))

        elif isinstance(expr, irast.MetaRef):
            inistep = self._process_expr(context, expr.ref)
            typstep = qlast.LinkExprNode(expr=qlast.LinkNode(name='__class__'))
            refstep = qlast.LinkExprNode(expr=qlast.LinkNode(name=expr.name))
            result = qlast.PathNode(steps=[inistep, typstep, refstep])

        elif isinstance(expr, irast.AtomicRefSimple):
            path = self._process_expr(context, expr.ref)
            link = qlast.LinkNode(name=expr.name.name, namespace=expr.name.module)
            link = qlast.LinkExprNode(expr=link)
            path.steps.append(link)
            result = path

        elif isinstance(expr, irast.AtomicRefExpr):
            result = self._process_expr(context, expr.expr)

        elif isinstance(expr, irast.EntitySet):
            links = []

            while expr.rlink and (not expr.show_as_anchor or context.inline_anchors):
                linknode = expr.rlink
                linkclass = linknode.link_class
                lname = linkclass.normal_name()

                target = linknode.target.concept.name
                target = qlast.ClassRefNode(name=target.name, module=target.module)
                link = qlast.LinkNode(name=lname.name, namespace=lname.module,
                                      direction=linknode.direction, target=target)
                link = qlast.LinkExprNode(expr=link)
                links.append(link)

                expr = expr.rlink.source

            path = qlast.PathNode()

            if expr.show_as_anchor and not context.inline_anchors:
                step = qlast.PathStepNode(expr=expr.show_as_anchor)
            else:
                step = qlast.PathStepNode(expr=expr.concept.name.name,
                                          namespace=expr.concept.name.module)

            path.steps.append(step)
            path.steps.extend(reversed(links))

            result = path

        elif isinstance(expr, irast.PathCombination):
            paths = list(expr.paths)
            if len(paths) == 1:
                result = self._process_expr(context, paths[0])
            else:
                assert False, "path combinations are not supported yet"

        elif isinstance(expr, irast.SubgraphRef):
            result = self._process_expr(context, expr.ref)

        elif isinstance(expr, irast.LinkPropRefSimple):
            if expr.show_as_anchor:
                result = qlast.PathStepNode(expr=expr.show_as_anchor)
            else:
                path = self._process_expr(context, expr.ref)

                # Skip transformed references to a multiatom link, as those are not actual
                # property refs.
                #
                if expr.name != 'std::target' or expr.ref.source is None:
                    link = qlast.LinkNode(name=expr.name.name, namespace=expr.name.module,
                                          type='property')
                    link = qlast.LinkPropExprNode(expr=link)
                    path.steps.append(link)

                result = path

        elif isinstance(expr, irast.LinkPropRefExpr):
            result = self._process_expr(context, expr.expr)

        elif isinstance(expr, irast.EntityLink):
            if expr.show_as_anchor and not context.inline_anchors:
                result = qlast.PathNode(steps=[qlast.PathStepNode(expr=expr.show_as_anchor)])
            else:
                if expr.source:
                    path = self._process_expr(context, expr.source)
                else:
                    path = qlast.PathNode()

                linkclass = expr.link_class
                lname = linkclass.normal_name()

                if expr.target and isinstance(expr.target, irast.EntitySet):
                    target = expr.target.concept.name
                    target = qlast.ClassRefNode(name=target.name, module=target.module)
                else:
                    target = None

                if path.steps:
                    link = qlast.LinkNode(name=lname.name, namespace=lname.module, target=target,
                                          direction=expr.direction)
                    link = qlast.LinkExprNode(expr=link)
                else:
                    link = qlast.LinkNode(name=lname.name, namespace=lname.module, target=target,
                                          direction=expr.direction)

                path.steps.append(link)
                result = path

        elif isinstance(expr, irast.Sequence):
            elements = [self._process_expr(context, e) for e in expr.elements]
            result = qlast.SequenceNode(elements=elements)

        elif isinstance(expr, irast.TypeCast):
            if expr.type.subtypes:
                typ = qlast.TypeNameNode(
                    maintype=qlast.ClassRefNode(name=expr.type.maintype),
                    subtypes=[
                        qlast.ClassRefNode(
                            module=stn.module, name=stn.name)
                        for stn in expr.type.subtypes
                    ]
                )
            else:
                mtn = expr.type.maintype
                mt = qlast.ClassRefNode(module=mtn.module, name=mtn.name)
                typ = qlast.TypeNameNode(maintype=mt)

            result = qlast.TypeCastNode(
                expr=self._process_expr(context, expr.expr), type=typ)

        elif isinstance(expr, irast.NoneTest):
            arg = self._process_expr(context, expr.expr)
            result = qlast.UnaryOpNode(
                operand=qlast.ExistsPredicateNode(expr=arg),
                op=qlast.NOT)

        elif isinstance(expr, irast.SliceIndirection):
            start = self._process_expr(context, expr.start)
            stop = self._process_expr(context, expr.stop)

            result = qlast.IndirectionNode(
                arg=self._process_expr(context, expr.expr),
                indirection=[
                    qlast.SliceNode(
                        start=(None if self._is_none(context, start) else start),
                        stop=(None if self._is_none(context, stop) else stop),
                    )
                ]
            )

        elif isinstance(expr, irast.IndexIndirection):
            result = qlast.IndirectionNode(
                arg=self._process_expr(context, expr.expr),
                indirection=[
                    qlast.IndexNode(
                        index=self._process_expr(context, expr.index),
                    )
                ]
            )

        else:
            assert False, "Unexpected expression type: %r" % expr

        return result


def decompile_ir(irtree, inline_anchors=False, return_statement=False):
    decompiler = IRDecompiler()
    return decompiler.transform(irtree, inline_anchors=inline_anchors,
                                        return_statement=return_statement)
