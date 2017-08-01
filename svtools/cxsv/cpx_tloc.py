#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright © 2017 Matthew Stone <mstone5@mgh.harvard.edu>
# Distributed under terms of the MIT license.

"""
Classification of reciprocal translocations.
"""


def classify_simple_translocation(plus, minus, mh_buffer=50):
    """
    Resolve a pair of interchromosomal breakends.

    Parameters
    ----------
    FF : pysam.VariantRecord
        FF inversion breakpoint.
    RR : pysam.VariantRecord
        RR inversion breakpoint.
    cnvs : list of pysam.VariantRecord
        List of overlapping CNVs.

    Returns
    -------
    svtype : str
        Complex SV class.
    """

    # plus refers to breakend whose strand begins with '+'
    if plus.chrom != minus.chrom or plus.info['CHR2'] != minus.info['CHR2']:
        return 'TLOC_MISMATCH_CHROM'

    # Reference chromosomes are labeled A and B
    # Breakpoints/Derivative chromosomes are labeled plus and minus, based on
    # ref chromosome A's strandedness on each breakpoint
    # plus_A = the breakend of ref chrom A on derivative chrom where A is
    # forward-stranded

    # get positions
    plus_A = plus.pos
    minus_A = minus.pos
    plus_B = plus.info['END']
    minus_B = minus.info['END']

    plus_strands = plus.info['STRANDS']

    # Buffer comparisons
    def _greater(p1, p2):
        return p1 > p2 - mh_buffer

    if plus_strands == '+-':
        if _greater(minus_A, plus_A) and _greater(plus_B, minus_B):
            return 'CTX_PP/QQ'
        if _greater(minus_A, plus_A) and _greater(minus_B, plus_B):
            return 'CTX_INS_B2A'
        if _greater(plus_A, minus_A) and _greater(plus_B, minus_B):
            return 'CTX_INS_A2B'
    else:
        if _greater(minus_A, plus_A) and _greater(minus_B, plus_B):
            return 'CTX_PQ/QP'
        if _greater(minus_A, plus_A) and _greater(plus_B, minus_B):
            return 'CTX_INV_INS_B2A'
        if _greater(plus_A, minus_A) and _greater(minus_B, plus_B):
            return 'CTX_INV_INS_A2B'

    return 'CTX_UNR'
