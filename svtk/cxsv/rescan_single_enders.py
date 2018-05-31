#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright © 2018 Matthew Stone <mstone5@mgh.harvard.edu>
# Distributed under terms of the MIT license.

"""

"""

import argparse
import itertools
from collections import defaultdict
import pysam
from svtk.genomeslink import GenomeSLINK, GSNode
import svtk.utils as svu


class DiscPair(GSNode):
    def __init__(self, chrA, posA, strandA, chrB, posB, strandB, sample):
        self.strandA = strandA
        self.strandB = strandB
        self.sample = sample
        super().__init__(chrA, posA, chrB, posB)

    @property
    def is_inversion(self):
        return (self.chrA == self.chrB) and (self.strandA == self.strandB)


def match_cluster(record, cluster, dist=300):
    """
    Determine whether DiscPair cluster matches a VCF record of interest.

    Checks if pairs exist which match the record's strandedness, then checks
    whether the min/max coord of these pairs is within a specified distance
    of the record's coordinates.

    Arguments
    ---------
    record : pysam.VariantRecord
    cluster : list of DiscPair
    dist : int, optional

    Returns
    -------
    match : bool
    """
    r_start, r_end = record.pos, record.stop

    if record.info['STRANDS'] == '++':
        # If no ++ pairs present, return False
        if not any(p.strandA == '+' for p in cluster):
            return False

        # Otherwise choose max start/end of ++ pairs
        c_start = max(p.posA for p in cluster if (p.strandA == '+'))
        c_end = max(p.posB for p in cluster if (p.strandB == '+'))
    elif record.info['STRANDS'] == '--':
        # If no -- pairs present, return False
        if not any(p.strandA == '-' for p in cluster):
            return False

        # Otherwise choose min start/end of -- pairs
        c_start = min(p.posA for p in cluster if (p.strandA == '-'))
        c_end = min(p.posB for p in cluster if (p.strandB == '-'))
    else:
        strands = record.info['STRANDS']
        raise Exception('Invalid inversion orientation: {0}'.format(strands))

    # Test if cluster start/end are sufficiently close to record start/end
    return abs(r_start - c_start) < dist and abs(r_end - c_end) < dist



def rescan_single_ender(record, pe, window=500, dist=300, min_support=4,
                        min_frac_samples=0.5):
    """
    Test if a putative single-ender inversion has support from other strand.

    Selects discordant pairs in the neighborhood of the original record, then
    clusters them together. If enough samples have sufficient paired-end 
    evidence supporting the opposite strand, we have found support for the
    other end of the record.

    Arguments
    ---------
    record : pysam.VariantRecord
    pe : pysam.TabixFile
        Scraped discordant pair metadata
    window : int, optional
        Window around record start to search for pairs
    dist : int, optional
        Clustering distance for fetched pairs
    min_support : int, optional
        Number of pairs required to count a sample as supported
    min_frac_samples : float, optional
        Fraction of called samples required to have opposite strand support in
        order to call the record as having both strands present. If 0, only one
        sample will be required.

    Returns
    -------
    opposite : pysam.VariantRecord
        Record corresponding to the pairs found supporting the other strand.
        None if no such pairs found
    """

    # Select pairs nearby record
    pairs = pe.fetch('{0}:{1}-{2}'.format(record.chrom, record.pos - window,
                                          record.pos + window))
    pairs = [DiscPair(*p.split()) for p in pairs]

    # Restrict to inversions in samples called in VCF record
    called = svu.get_called_samples(record)
    pairs = [p for p in pairs if p.sample in called and p.is_inversion]

    # Cluster pairs
    slink = GenomeSLINK(pairs, dist)
    clusters = [c for c in slink.cluster() if match_cluster(record, c, window)]

    # If no clusters, fail site, otherwise choose largest cluster
    if len(clusters) == 0:
        return None
    else:
        cluster = max(clusters, key=len)

    # Select clustered pairs which support the opposite strand as the record
    missing_strand = '+' if record.info['STRANDS'] == '--' else '-'
    supporting_pairs = [p for p in cluster if p.strandA == missing_strand]

    # Count number of supporting pairs in each called sample
    sample_support = defaultdict(int)
    for pair in supporting_pairs:
        sample_support[pair.sample] += 1
   
    # If enough samples were found to have support, make new variant record
    n_supported_samples = sum(sample_support[s] >= min_support for s in called)
    if n_supported_samples / len(called) >= min_frac_samples:
        return make_new_record(supporting_pairs, record)
    else:
        return None


def make_new_record(pairs, old_record):
    record = old_record.copy()
    
    record.id = record.id + '_OPPSTRAND'

    if pairs[0].strandA == '+':
        record.pos = max(p.posA for p in pairs)
        record.stop = max(p.posB for p in pairs)
        record.info['STRANDS'] = '++'
    else:
        record.pos = min(p.posA for p in pairs)
        record.stop = min(p.posB for p in pairs)
        record.info['STRANDS'] = '--'

    record.info['SVLEN'] = record.stop - record.pos
    record.info['ALGORITHMS'] = ('rescan',)

    return record


def rescan_single_enders(vcf, pe, window=500):
    for record in vcf:
        rescan_single_ender(record, pe, window)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('vcf', help='Single enders')
    parser.add_argument('pairs', help='Scraped discordant pair file.')
    parser.add_argument('--window', type=int, default=500, help='Window around '
                        'single ender coordinates to search for pairs')
    args = parser.parse_args()

    vcf = pysam.VariantFile(args.vcf)
    pe = pysam.TabixFile(args.pairs)

    rescan_single_enders(vcf, pe)


if __name__ == '__main__':
    main()
