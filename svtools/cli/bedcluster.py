#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright © 2016 Matthew Stone <mstone5@mgh.harvard.edu>
# Distributed under terms of the MIT license.

"""
Cluster a bed produced by a bedtools intersection of a bed with itself.

A self-intersected bed has two sets of columns in each row, corresponding to
two entries in the original bed that shared sufficient overlap. The clustering
is performed by linking each such pair and then returning all clusters of
linked calls.
"""

import argparse
import sys
from collections import namedtuple, deque, defaultdict
import numpy as np
from scipy import sparse
from scipy.sparse import csgraph
import pybedtools as pbt


BedCall = namedtuple('BedCall', 'chrom start end name sample svtype'.split())


def rmsstd(intervals):
    starts = np.array([interval.start for interval in intervals])
    ends = np.array([interval.end for interval in intervals])

    def _meanSS(X):
        mu = np.mean(X)
        return np.sum((X - mu) ** 2) / len(X)

    SS = _meanSS(starts) + _meanSS(ends)
    return np.sqrt(SS)


def bedcluster(bed, frac=0.8):
    """
    Single linkage clustering of a bed file based on reciprocal overlap.

    Parameters
    ----------
    bed : pybedtools.BedTool
        Columns: chr, start, end, name, sample, svtype.
    frac : float
        Minimum reciprocal overlap for two variants to be linked together.

    Returns
    -------
    clusters : list of deque of pybedtools.Interval
    """

    # Get list of unique variant IDs and initialize sparse graph
    variant_IDs = [interval.fields[3] for interval in bed.intervals]
    G = sparse.eye(len(variant_IDs), dtype=np.uint16, format='lil')

    # Map variant IDs to graph indices
    variant_indexes = {}
    for i, variant in enumerate(variant_IDs):
        variant_indexes[variant.strip()] = i

    # Self-intersect the bed
    intersection = bed.intersect(bed, wa=True, wb=True, loj=True,
                                 r=True, f=frac)

    # Cluster intervals based on reciprocal overlap
    for interval in intersection.intervals:
        # Make fields accessible by name
        c1 = BedCall(*interval.fields[:6])
        c2 = BedCall(*interval.fields[6:])

        # Link the two calls from the current line
        if c2.chrom != '.' and c1.svtype == c2.svtype:
            idx1 = variant_indexes[c1.name]
            idx2 = variant_indexes[c2.name]
            G[idx1, idx2] = 1

    # Cluster graph
    n_comp, cluster_labels = csgraph.connected_components(G, connection='weak')

    # Build deques of clustered Intervals
    clusters = [deque() for i in range(n_comp)]
    for idx, interval in enumerate(bed.intervals):
        label = cluster_labels[idx]
        clusters[label].append(interval)

    return clusters


def collapse_sample_calls(cluster):
    """
    Merges multiple variants in same sample

    Parameters
    ----------
    cluster : list of pybedtools.Interval

    Returns
    -------
    cluster : list of pybedtools.Interval
    """

    interval_dict = defaultdict(list)
    variants = deque()

    # Get all calls in each sample
    for interval in cluster:
        sample = interval.fields[4]
        interval_dict[sample].append(interval)

    # If a sample has only one call, keep it, otherwise merge
    for sample, intervals in interval_dict.items():
        if len(intervals) == 1:
            variants.append(intervals[0])
            continue

        # Track IDs of merged variants
        name = ','.join([interval.name for interval in intervals])

        # To merge variants in a sample, take broadest range
        start = np.min([interval.start for interval in intervals])
        end = np.max([interval.end for interval in intervals])

        interval = intervals[0]
        interval.start = start
        interval.end = end
        interval.name = name

        variants.append(interval)

    return list(variants)


def main(argv):
    parser = argparse.ArgumentParser(
        description=__doc__,
        prog='svtools bedcluster',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('bed', help='SV calls to cluster. Columns: #chr, '
                        'start, end, name, sample, svtype')
    parser.add_argument('-f', '--frac', type=float, default=0.8,
                        help='Minimum reciprocal overlap fraction to link '
                        'variants. [0.8]')
    parser.add_argument('-r', '--region', help='Region to cluster '
                        '(chrom:start-end). Requires tabixed bed.')
    parser.add_argument('-p', '--prefix', default='prefix',
                        help='Cluster ID prefix')
    parser.add_argument('-m', '--merge-coordinates',
                        action='store_true', default=False,
                        help='Report median of start and end positions in '
                        'each cluster as final coordinates of cluster.')
    parser.add_argument('fout', type=argparse.FileType('w'),
                        nargs='?', default=sys.stdout,
                        help='Clustered bed.')
    args = parser.parse_args(argv)

    bed = pbt.BedTool(args.bed)
    if args.region:
        bed = bed.tabix_intervals(args.region)

    # Drop any columns beyond those required
    bed = bed.cut(range(6))

    header = ('#chrom start end name svtype sample call_name vaf vac '
              'pre_rmsstd post_rmsstd')
    header = '\t'.join(header.split()) + '\n'
    args.fout.write(header)

    clusters = bedcluster(bed, args.frac)

    # Get samples for VAF calculation
    samples = sorted(set([interval.fields[4] for interval in bed.intervals]))
    num_samples = float(len(samples))

    for i, cluster in enumerate(clusters):
        # Calculate RMSSTD before merging per-sample variants
        pre_RMSSTD = rmsstd(cluster)

        # Make a single variant for each sample
        cluster = collapse_sample_calls(cluster)

        # Re-calculate RMSSTD after merging per-sample variants
        post_RMSSTD = rmsstd(cluster)

        # Merge coordinates AFTER getting min/max per sample
        if args.merge_coordinates:
            # Report median region of overlap
            start = int(np.median([int(call.start) for call in cluster]))
            end = int(np.median([int(call.end) for call in cluster]))

            for interval in cluster:
                interval.start = start
                interval.end = end

        # Get variant frequency info
        vac = len(set([call.fields[4] for call in cluster]))
        vaf = vac / num_samples

        # Assign cluster ID
        cid = args.prefix + ('_%d' % i)

        for interval in cluster:
            entry = '{0}\t{1}\t{2}\t{{cid}}\t{5}\t{4}\t{3}'
            entry = entry.format(*interval.fields)

            entry = (entry + '\t{vaf:.3f}\t{vac}\t{pre_RMSSTD:.3f}\t'
                     '{post_RMSSTD:.3f}\n').format(**locals())

            args.fout.write(entry)


if __name__ == '__main__':
    main(sys.argv[1:])
