#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright © 2017 Matthew Stone <mstone5@mgh.harvard.edu>
# Distributed under terms of the MIT license.

"""
Calculate enrichment of clipped reads at SV breakpoints.
"""

import argparse
import sys
import io
from collections import deque
import numpy as np
import scipy.stats as ss
import pandas as pd
import pysam
import svtools.pesr as pesr


class SRBreakpoint(pesr.Breakpoint):
    def load_counts(self, countfile, window):
        """
        Generate dataframe of split counts

        Parameters
        ----------
        countfile : pysam.TabixFile
            chrom pos clip count sample
        window : int
        """

        reg = '{0}:{1}-{2}'

        # Check if regions overlap so duplicate lines aren't fetched
        if (self.chrA == self.chrB) and (self.posB - self.posA <= 2 * window):
            regions = [
                reg.format(self.chrA, self.posA - window, self.posB + window)
            ]
        else:
            regions = [
                reg.format(self.chrA, self.posA - window, self.posA + window),
                reg.format(self.chrB, self.posB - window, self.posB + window),
            ]

        counts = deque()
        for region in regions:
            lines = countfile.fetch(region=region)
            lines = [l for l in lines]
            counts.append('\n'.join(lines))

        counts = io.StringIO('\n'.join(counts))

        cols = 'chrom pos clip count sample'.split()
        dtypes = dict(chrom=str, pos=int, clip=str, count=int, sample=str)
        self.split_counts = pd.read_table(counts, names=cols, dtype=dtypes)

    def process_counts(self, window):
        """
        Filter to called/background samples and assign coordinates

        Parameters
        ----------
        window : int
        """

        counts = self.split_counts

        # Restrict to called or background samples
        samples = self.samples + self.background
        counts = counts.loc[counts['sample'].isin(samples)].copy()

        # Determine whether clipped read supports start or end
        self.add_coords(counts)

        # Filter to within window
        # (excludes spurious left/right clips at other coord)
        counts = counts.loc[counts['dist'].abs() <= window].copy()

        self.split_counts = counts

        # Fill empty samples
        self.fill_counts()

        # Label samples with background
        is_called = self.split_counts['sample'].isin(self.samples)
        if is_called.any():
            self.split_counts.loc[is_called, 'call_status'] = 'called'
        if (~is_called).any():
            self.split_counts.loc[~is_called, 'call_status'] = 'background'

    def add_coords(self, df):
        df['posA'] = (df.pos - self.posA).abs()
        df['posB'] = (df.pos - self.posB).abs()

        strandA, strandB = self.strands

        # Map clip direction to strandedness
        clip_map = {'right': '+', 'left': '-'}
        df['strand'] = df['clip'].replace(clip_map)

        # If strands match, pick closest pos
        if strandA == strandB:
            cols = 'posA posB'.split()
            df['coord'] = df[cols].idxmin(axis=1)
        # Else match clip to position strand
        else:
            coord_map = {strandA: 'posA', strandB: 'posB'}
            df['coord'] = df['strand'].replace(coord_map)

        # Choose dist to identified coord
        df['dist'] = df.lookup(df.index, df.coord)

        if strandA == strandB:
            df.loc[df.strand != strandA, 'dist'] = np.inf

    def add_dists(self, df):
        # Get distance of clip position from variant start/end
        def _coord_dist(row):
            coord = row['coord']  # 'start' or 'end'
            pos = getattr(self, coord)
            return pos - row.pos
        df['dist'] = df.apply(_coord_dist, axis=1)

    def fill_counts(self):
        """
        Fill zeros in for samples with no observed splits
        """
        counts = self.split_counts
        samples = self.samples + self.background

        sub_dfs = []
        cols = 'sample pos count'.split()

        for coord in 'posA posB'.split():
            # Filter tlocs
            chrom = getattr(self, 'chr' + coord[-1])
            df = counts.loc[(counts.coord == coord) &
                            (counts.chrom == chrom), cols]

            # Consider only positions found in a called sample
            pos = df.loc[df['sample'].isin(self.samples), 'pos']
            pos = pos.unique()

            idx = pd.MultiIndex.from_product(iterables=[samples, pos])

            df = df.set_index('sample pos'.split())
            df = df.reindex(idx).fillna(0).astype(int).reset_index()
            df = df.rename(columns=dict(level_0='sample', level_1='pos'))

            df['coord'] = coord
            sub_dfs.append(df)

        self.split_counts = pd.concat(sub_dfs)

    def test_counts(self):
        pvals = self.split_counts.groupby('coord pos'.split())\
                                 .apply(self.calc_test)

        cols = {
            0: 'called_median',
            1: 'bg_median',
            2: 'log_pval'
        }

        self.pvals = pvals.rename(columns=cols).reset_index()

    @staticmethod
    def calc_test(df):
        statuses = 'called background'.split()
        medians = df.groupby('call_status')['count'].median()
        medians = medians.reindex(statuses).fillna(0)  # .round().astype(int)

        pval = ss.poisson.cdf(medians.background, medians.called)

        return pd.Series([medians.called, medians.background, -np.log10(pval)])

    def choose_best_coords(self):
        # Pick coordinates with most significant enrichment
        max_pvals = self.pvals.groupby('coord')['log_pval'].max()\
                         .reset_index()\
                         .rename(columns={'log_pval': 'max_pval'})

        pvals = pd.merge(self.pvals, max_pvals, on='coord', how='left')
        pvals = pvals.loc[pvals.log_pval == pvals.max_pval].copy()
        pvals = pvals.drop('max_pval', axis=1)

        for coord in 'posA posB'.split():
            if coord not in pvals.coord.values:
                pvals = pd.concat([pvals, self.null_series(coord)])

        # Use distance as tiebreaker
        if pvals.shape[0] > 2:
            self.add_dists(pvals)
            closest = pvals.groupby('coord')['dist'].min().reset_index()\
                           .rename(columns={'dist': 'min_dist'})
            pvals = pd.merge(pvals, closest, on='coord', how='left')
            pvals = pvals.loc[pvals.dist == pvals.min_dist].copy()
            pvals = pvals.drop('dist min_dist'.split(), axis=1)

        pvals['name'] = self.name
        self.best_pvals = pvals

    def normalize_counts(self, cov):
        counts = self.split_counts
        counts = pd.merge(counts, cov, on='sample', how='left')
        counts['norm_count'] = counts['count'] / counts['MEDIAN_COVERAGE']
        counts = counts['sample pos norm_count coord call_status'.split()]
        counts = counts.rename(columns={'norm_count': 'count'})
        self.split_counts = counts

    def sr_test(self, samples, countfile, n_background, window, cov=None):
        # Choose background samples
        self.choose_background(samples, n_background)

        # Load counts and return null score if no splits found
        self.load_counts(countfile, window)
        if self.split_counts.shape[0] == 0:
            self.null_score()
            return

        # Filter counts and return null score if no splits left
        self.process_counts(window)
        if self.split_counts.shape[0] == 0:
            self.null_score()
            return

        if cov is not None:
            self.normalize_counts(cov)

        # Test sites for significant enrichment of splits and return best
        self.test_counts()
        self.choose_best_coords()
        self.test_total()

    def test_total(self):
        pvals = self.best_pvals.set_index('coord')
        posA = pvals.loc['posA'].pos
        posB = pvals.loc['posB'].pos

        counts = self.split_counts
        mask = (((counts.coord == 'posA') & (counts.pos == posA)) |
                ((counts.coord == 'posB') & (counts.pos == posB)))

        counts = counts.loc[mask]
        totals = counts.groupby('sample call_status'.split())['count'].sum()
        pvals = self.calc_test(totals.reset_index()).to_frame().transpose()

        cols = {
            0: 'called_median',
            1: 'bg_median',
            2: 'log_pval'
        }

        pvals = pvals.rename(columns=cols)
        pvals['coord'] = 'sum'
        pvals['pos'] = 0
        pvals['name'] = self.name

        self.best_pvals = pd.concat([self.best_pvals, pvals])

    def null_score(self):
        self.best_pvals = pd.concat(
            [self.null_series('posA'),
             self.null_series('posB'),
             self.null_series('sum')])

    def null_series(self, coord):
        cols = 'name coord pos called_median bg_median log_pval'.split()
        return pd.DataFrame([[self.name, coord, 0, 0, 0, 0]], columns=cols)


def _BreakpointParser(variantfile, bed=False):
    """
    variantfile : pysam.Tabixfile or file
    bed : bool, optional
        variants is a bed
    """

    def _strand_check(record):
        return ('STRANDS' in record.info.keys() and
                record.info['STRANDS'] in '++ +- -+ --'.split())

    for record in variantfile:
        # Skip non-stranded variants (e.g. WHAM inversions)
        # TODO: log skipped records
        if not _strand_check(record):
            continue

        if bed:
            yield SRBreakpoint.from_bed(*record.strip().split()[:6])
        else:
            yield SRBreakpoint.from_vcf(record)


class SRTest():
    def __init__(self, variantfile, countfile, bed=False, samples=None,
                 window=100, n_background=160, cov=None, statsfile=None):
        """
        variantfile : file
            filepath of variants file
        countfile : pysam.TabixFile
            per-coordinate, per-sample split counts
            chrom pos clip count sample
        bed : bool, optional
            Variants file is a BED, not a VCF.
            BED columns: chrom start end name samples svtype
        samples : list of str, optional
            List of all samples to consider. By default, all samples in VCF
            header are considered. Required when specifying `bed=True`.
        window : int, optional
            Window around breakpoint to consider for split read enrichment
        n_background : int, optional
            Number of background samples to choose for comparison in t-test
        cov : dict of {str: int}, optional
            Per-sample median coverage. Split counts will be normalized by
            these values if provided.
        statsfile : file, optional
            If provided, write per-sample stats to disk
        """

        if bed:
            if samples is None:
                msg = 'samples is required when providing calls in BED format.'
                raise ValueError(msg)
        else:
            variantfile = pysam.VariantFile(variantfile)
            samples = list(variantfile.header.samples)

        self.breakpoints = _BreakpointParser(variantfile, bed)

        self.countfile = countfile
        self.samples = sorted(samples)

        self.window = window
        self.n_background = n_background
        self.pvals = None
        self.cov = cov
        self.statsfile = statsfile

    def run(self):
        for breakpoint in self.breakpoints:
            breakpoint.sr_test(self.samples, self.countfile, self.n_background,
                               self.window, self.cov)
            pvals = breakpoint.best_pvals
            cols = 'name coord pos log_pval called_median bg_median'.split()
            pvals = pvals[cols].fillna(0)

            int_cols = ['pos']  # called_median bg_median'.split()
            for col in int_cols:
                pvals[col] = pvals[col].round().astype(int)
            #  pvals['pos'] = pvals['pos'].astype(int)
            pvals.log_pval = np.abs(pvals.log_pval)

            import ipdb
            ipdb.set_trace()

            if self.statsfile:
                breakpoint.write_counts(self.statsfile)

            yield pvals


def main(argv):
    parser = argparse.ArgumentParser(
        description=__doc__,
        prog='svtools sr-test',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('variants',
                        help='VCF of variant calls. Standardized to include '
                        'CHR2, END, SVTYPE, STRANDS in INFO.')

    parser.add_argument('--bed', action='store_true', default=False,
                        help='Variants file is in bed format. First six cols: '
                        'chrom,start,end,name,samples,svtype')
    parser.add_argument('--samples', type=argparse.FileType('r'), default=None,
                        help='File listing all sample IDs. Parsed from VCF '
                        'header by default, required for --bed.')

    # TODO: permit direct querying around bams
    parser.add_argument('counts', help='Tabix indexed file of split counts. '
                        'Columns: chrom,pos,clip,count,sample')

    parser.add_argument('fout', type=argparse.FileType('w'),
                        help='Output table of most significant start/end'
                        'positions.')

    parser.add_argument('-w', '--window', type=int, default=100,
                        help='Window around variant start/end to consider for '
                        'split read support. [100]')
    parser.add_argument('-b', '--background', type=int, default=160,
                        help='Number of background samples to choose for '
                        'comparison in t-test. [160]')
    parser.add_argument('--coverage-csv', default=None,
                        help='Median coverage statistics for each library '
                        '(optional). If provided, each sample\'s split counts '
                        'will be normalized accordingly. CSV: '
                        'sample,MEDIAN_COVERAGE,MAD_COVERAGE')

    # Print help if no arguments specified
    if len(argv) == 0:
        parser.print_help()
        sys.exit(1)
    args = parser.parse_args(argv)

    if args.samples is None:
        samples = None
        if args.bed:
            msg = '--samples is required when providing calls in BED format.'
            raise argparse.ArgumentError(msg)
    else:
        samples = [s.strip() for s in args.samples.readlines()]

    if args.variants in ['-', 'stdin']:
        variantfile = sys.stdin
    else:
        variantfile = open(args.variants)

    if args.coverage_csv is not None:
        cov = pd.read_csv(args.coverage_csv)
        required_cols = 'sample MEDIAN_COVERAGE'.split()
        for required_col in required_cols:
            if required_col not in cov.columns:
                msg = 'Required column {0} not in coverage csv'
                raise ValueError(msg)

        cov = cov[required_cols]
        #  cov = pd.Series(cov.MEDIAN_COVERAGE.values, index=cov['sample'])
        #  cov = cov.to_dict()
    else:
        cov = None

    countfile = pysam.TabixFile(args.counts)

    srtest = SRTest(variantfile, countfile, args.bed, samples,
                    args.window, args.background, cov)

    header = 'name coord pos log_pval called_median bg_median'.split()
    args.fout.write('\t'.join(header) + '\n')

    for pvals in srtest.run():
        pvals.to_csv(args.fout, sep='\t', index=False, header=False)


if __name__ == '__main__':
    main(sys.argv[1:])
