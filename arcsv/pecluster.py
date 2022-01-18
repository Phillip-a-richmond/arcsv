import igraph
import itertools
import functools
import numpy as np
import os
import pyinter
import sys
from bisect import bisect_left
from operator import attrgetter

from arcsv.breakpoint_merge import Breakpoint
from arcsv.helper import normcdf, not_primary, load_genome_gaps
from arcsv.unif_ci import uniform_ci


class DiscordantPair:
    def __init__(self, chrom, pos1, pos2, insert, qname):
        self.chrom = chrom
        self.pos1 = pos1
        self.pos2 = pos2
        self.insert = insert
        self.qname = qname

    def __repr__(self):
        return str((self.chrom, self.pos1, self.pos2, self.insert))

    def __hash__(self):
        return hash((self.chrom, self.pos1, self.pos2, self.insert, self.qname))

    def __lt__(self, other):
        return (self.pos1, self.pos2) < (other.pos1, other.pos2)

    def __gt__(self, other):
        return (self.pos1, self.pos2) > (other.pos1, other.pos2)


def process_discordant_pair(aln1, aln2, chrom, discordant_pairs, min_mapq, ilen,
                            min_insert, max_insert, is_rf=False):
    if (aln1.is_reverse != aln2.is_reverse) and (ilen is not None) and \
       (ilen >= min_insert) and (ilen <= max_insert):
        return None
    if aln1.mapq < min_mapq or aln2.mapq < min_mapq or aln1.is_unmapped or \
       aln2.is_unmapped or not_primary(aln1) or not_primary(aln2):
        return None
    # "First" is -> if FR (->  <-) and <- if RF (<-  ->)
    # i.e. the read we expect on the "left" in ref. coords
    if aln1.is_reverse != aln2.is_reverse:
        second = aln1 if (aln1.is_reverse ^ is_rf) else aln2
        first = aln1 if second is aln2 else aln2
        if ilen > max_insert and (first.reference_end < second.reference_start):
            # second check is needed for libraries with huge number of readthroughs,
            # e.g. with 2 x 150 reads and insert size cutoff < 300
            # TODO add conditions on other discordant read types?
            dtype = 'Del'
            disc = DiscordantPair(chrom, first.reference_end, second.reference_start,
                                  ilen, first.qname)
        elif (first.reference_start > second.reference_start) or \
             (first.reference_end > second.reference_end):
            dtype = 'Dup'
            disc = DiscordantPair(chrom, second.reference_start, first.reference_end,
                                  ilen, second.qname)
        elif ilen < min_insert:
            dtype = 'Ins'
            disc = DiscordantPair(chrom, first.reference_end, second.reference_start,
                                  ilen, first.qname)
    else:
        dtype = 'InvR' if (aln1.is_reverse ^ is_rf) else 'InvL'
        if dtype == 'InvL':
            pos1, pos2 = sorted([aln1.reference_end, aln2.reference_end])
        else:
            pos1, pos2 = sorted([aln1.reference_start, aln2.reference_start])
        disc = DiscordantPair(chrom, pos1, pos2, ilen, aln1.qname)
    discordant_pairs[dtype] = discordant_pairs.get(dtype, []) + [disc]
    if disc.pos1 > disc.pos2 and dtype != 'Ins':
        raise Warning('[process_disc_pair] discordant type {0} pos1 > pos2'.format(dtype))
    return dtype


# for each library in discordant_pairs_list, determine SV-specific
# cutoffs and cluster the discordant PE reads
# LATER allow to combine libraries of the same "type"
def apply_discordant_clustering(opts, discordant_pairs_list,
                                insert_mu, insert_sigma,
                                insert_min, insert_max, gap_file,
                                lr_cond=False, bp_confidence_level=0.95):
    nlib = opts['nlib']
    breakpoints = []
    for i in range(nlib):
        lib_name = opts['library_names'][i]
        for (dtype, pairs) in discordant_pairs_list[i].items():
            if opts['verbosity'] > 0:
                print('[pecluster] clustering {0}'.format(dtype))
            clusters, pairs_clustered = cluster_pairs(opts, pairs, dtype, i,
                                                      insert_mu[i], insert_sigma[i])

            if opts['verbosity'] > 0:
                print('[pecluster] computing null distribution for {0} clusters'.format(dtype))
            lr_null_clusters = compute_null_dist(opts, pairs_clustered, dtype,
                                                 insert_mu[i], insert_sigma[i],
                                                 gap_file, lib_idx=i, lr_cond=lr_cond)

            insert_cutoff = insert_max[i] if dtype == 'Del' else insert_min[i]
            fdc_out = fdr_discordant_clusters(opts, clusters, lr_null_clusters, dtype,
                                              insert_mu[i], insert_sigma[i],
                                              insert_cutoff, lr_cond)
            clusters_pass, clusters_fail, lr_pairs, first_reject = fdc_out
            for cl in clusters_pass:
                # print('passing cluster:')
                # print(cl)
                breakpoints.extend(cluster_to_bp(cl, bp_confidence_level, dtype, lib_name))
                # print(breakpoints[-2])
                # print(breakpoints[-1])
                # print('')
            if opts['verbosity'] > 0:
                print('[pecluster] {0}: {1} discordant {2} reads'
                      .format(opts['library_names'][i], len(pairs), dtype))
                print('[pecluster] {0}: {1} clusters, {2} passing {3} failing'
                      .format(opts['library_names'][i], dtype,
                              len(clusters_pass), len(clusters_fail)))
            outname = '{0}_{1}_cluster.txt'.format(lib_name, dtype)
            fname = os.path.join(opts['outdir'], 'logging', outname)
            write_clustering_results(fname, lr_pairs, first_reject)

    return breakpoints


def is_deldupinv_compatible(opts, pairs, max_distance,
                            insert_mu=None, insert_sigma=None, adjust=None):
    # close and intersecting?
    min_pos1 = min(p.pos1 for p in pairs)
    max_pos1 = max(p.pos1 for p in pairs)
    min_pos2 = min(p.pos2 for p in pairs)
    max_pos2 = max(p.pos2 for p in pairs)
    return max(max_pos1 - min_pos1, max_pos2 - min_pos2) <= max_distance \
        and max_pos1 < min_pos2


def is_ins_compatible(opts, pairs, max_distance, insert_mu, insert_sigma):
    # if adjust:
    #     est_insertion_size = insert_mu - (pair1.insert + pair2.insert)/2
    #     overlap = max(0, max(pair1.pos1, pair2.pos1) - min(pair1.pos2, pair2.pos2))
    #     adjustment = max(0, est_insertion_size - overlap - 3/sqrt(2)*insert_sigma)
    #     # TODO use this
    #     adjusted_max_distance = max_distance - adjustment
    #     # if adjustment > 0:
    #     #     print('ins_compatible adjust mu={0} est={1} adjusted={2}'
    #     #           .format(insert_mu, est_insertion_size, adjusted_max_distance))
    # else:
    #     adjusted_max_distance = max_distance  # INSERTIONS THESIS
    min_pos1 = min(p.pos1 for p in pairs)
    max_pos1 = max(p.pos1 for p in pairs)
    min_pos2 = min(p.pos2 for p in pairs)
    max_pos2 = max(p.pos2 for p in pairs)

    is_close = max(max_pos1 - min_pos1, max_pos2 - min_pos2) <= max_distance
    overlap = max(0, max_pos1 - min_pos2)

    # no intersection requirement because possible homologous flanking sequences
    return is_close and overlap <= opts['max_ins_pair_slop']


def is_ins_cluster_compatible(opts, cluster):
    overlap = max(0, max([p.pos1 for p in cluster]) - min([p.pos2 for p in cluster]))
    # LATER do we need this or is it guaranteed?
    return overlap <= opts['max_ins_cluster_slop']


compatibility_fun = {'Del': is_deldupinv_compatible,
                     'Ins': is_ins_compatible,
                     'Dup': is_deldupinv_compatible,
                     'InvR': is_deldupinv_compatible,
                     'InvL': is_deldupinv_compatible}


def cluster_pairs(opts, pairs, dtype, lib_idx, insert_mu, insert_sigma):
    pairs.sort(key=attrgetter('pos1'))
    max_compatible_distance = insert_mu + opts['cluster_max_distance_sd'] * insert_sigma
    max_cluster_size = opts['max_pecluster_size'][lib_idx]
    is_compatible = functools.partial(compatibility_fun[dtype],
                                      opts=opts,
                                      max_distance=max_compatible_distance,
                                      insert_mu=insert_mu, insert_sigma=insert_sigma)
    if opts['verbosity'] > 1:
        print('clustering {0} pairs'.format(dtype))
        print('max cluster size: {0}'.format(max_cluster_size))

    cur_comps = []              # pairs in the current connected components
    cur_maxpos = []             # max(pair.pos1) over pairs in cur_comps
    clusters = []               # list of components
    pairs_clustered = []
    for pair in pairs:
        # check for components we've moved past
        passed_comps = [i for i in range(len(cur_comps)) if
                        abs(cur_maxpos[i] - pair.pos1) > max_compatible_distance]
        if passed_comps != sorted(passed_comps):
            raise Warning('passed_comps not sorted? {0}'.format(passed_comps))
        offset = 0
        for i in passed_comps:
            idx = i - offset    # adjust for deleting other stuff
            # print('passed component with maxpos %d' % cur_maxpos[idx])

            if len(cur_comps[idx]) > max_cluster_size and \
               is_compatible(pairs=cur_comps[idx]):
                # for huge clusters, we just skip unless all discordant pairs
                #   are mutually compatible
                new_clusters = [cur_comps[idx]]
            elif 1 < len(cur_comps[idx]) <= max_cluster_size:
                new_clusters = cluster_handle_component(cur_comps[idx], is_compatible)
            else:
                new_clusters = []
            clusters.extend(new_clusters)
            # add to list of pairs to use for null distribution simulation,
            #   unless this is a huge cluster we skipped
            if len(new_clusters) > 0 or len(cur_comps[idx]) == 1:
                pairs_clustered.extend(cur_comps[idx])

            del cur_comps[idx]
            del cur_maxpos[idx]
            offset += 1
        # check whether pair is connected to existing components
        adjacent_comps = [i for i in range(len(cur_comps))
                          if any(is_compatible(pairs=(pair, p))
                                 for p in reversed(cur_comps[i]))]
        # add pair to existing component, else make new component
        if len(adjacent_comps) > 0:
            cur_comps[adjacent_comps[0]].append(pair)
            cur_maxpos[adjacent_comps[0]] = max(cur_maxpos[adjacent_comps[0]], pair.pos1)
        else:
            cur_comps.append([pair])
            cur_maxpos.append(pair.pos1)
        # and merge adjacent comps if necessary
        if len(adjacent_comps) > 1:
            merged = list(itertools.chain(*(cur_comps[i] for i in adjacent_comps)))
            merged_maxpos = max(cur_maxpos[i] for i in adjacent_comps)
            offset = 0
            for i in adjacent_comps:
                del cur_comps[i - offset]
                del cur_maxpos[i - offset]
                offset += 1
            cur_comps.append(merged)
            cur_maxpos.append(merged_maxpos)
    # handle remaining components
    for comp in cur_comps:
        if len(comp) > max_cluster_size and \
           is_compatible(pairs=comp):
            new_clusters = [comp]
        elif 1 < len(comp) <= max_cluster_size:
            new_clusters = cluster_handle_component(comp, is_compatible)
        else:
            new_clusters = []
        clusters.extend(new_clusters)
        if len(new_clusters) > 0 or len(comp) == 1:
            pairs_clustered.extend(comp)

    # insertions need some additional filtering
    if dtype == 'Ins':
        clusters = [c for c in clusters if is_ins_cluster_compatible(opts, c)]
    return clusters, pairs_clustered


# component: guaranteed length >= 2
def cluster_handle_component(component, is_compatible):
    # print('handling component:\n\t%s' % component)
    # create a graph and fill in all the edges
    g = igraph.Graph(len(component))
    g.vs['pairs'] = component
    iter_pairs = range(len(component))
    compatible_pairs = [(i, j) for (i, j) in itertools.product(iter_pairs, repeat=2) if
                        i != j and is_compatible(pairs=(component[i], component[j]))]
    for cp in compatible_pairs:
        g.add_edge(*cp)
    # get max cliques and add to result
    result = []
    for clique in g.largest_cliques():
        # print('\tclique of size %d' % len(clique))
        result.append(g.vs[clique]['pairs'])
    return result


# discordant_pairs: list of tuples corresponding to discordant pairs
# non_gaps: list of intervals where we can place the discordant pairs
def shuffle_discordant_pairs(discordant_pairs, chrom_len_no_gaps, max_insert_size=np.Inf):
    shuffled = []
    for pair in discordant_pairs:
        if max_insert_size < np.Inf and pair.insert > max_insert_size:
            continue
        pair_len = pair.pos2 - pair.pos1
        # ignoring read length, but doesn't matter for chrom_len >> read_len
        if pair_len < chrom_len_no_gaps and pair_len > -chrom_len_no_gaps:
            new_pos1 = np.random.random_integers(max(0, -pair_len),
                                                 chrom_len_no_gaps - max(0, pair_len))
            new_pair = DiscordantPair(pair.chrom, new_pos1, new_pos1 + pair_len,
                                      pair.insert, pair.qname)
            shuffled.append(new_pair)
        else:
            continue
    return shuffled


# LATER proper mle with empirical dist
# LATER LR including proportion of discordant reads in this area
# cluster: list of mutually compatible del-type discordants
def lr_del(cluster, insert_mu, insert_sigma, cutoff, conditioning=False):
    max_size = min([p.pos2 for p in cluster]) - max([p.pos1 for p in cluster])
    del_size_mle = min(max_size, np.mean([pair.insert for pair in cluster]) - insert_mu)

    lr = sum([(p.insert - insert_mu)**2 for p in cluster]) \
        - sum([(p.insert - insert_mu - del_size_mle)**2 for p in cluster])
    lr = lr / (2 * insert_sigma**2)

    if lr == -np.inf:
        sys.stderr.write('[lr_del] DEL likelihood ratio = -inf\n')
        sys.stderr.write('{0}\n'.format(cluster))

    if conditioning:
        n = len(cluster)
        lr += n * (np.log(1 - normcdf(cutoff, insert_mu, insert_sigma))
                   - np.log(1 - normcdf(cutoff - del_size_mle, insert_mu, insert_sigma)))

    return lr


def lr_ins(cluster, insert_mu, insert_sigma, cutoff, conditioning=False):
    # -->   <--
    #      -->   <--
    # overlap like this is possible with homologous sequences flanking insertion
    overlap = max(0, max([p.pos1 for p in cluster]) - min([p.pos2 for p in cluster]))
    ins_size_mle = max(overlap, insert_mu - np.mean([pair.insert for pair in cluster]))
    # if overlap > 0:
    #     print('insertion cluster w/overlap: {0}'.format(cluster))
    #     print('overlap {0} mle {1}\n'.format(overlap, ins_size_mle))

    lr = sum([(p.insert - insert_mu)**2 for p in cluster]) \
        - sum([(p.insert - insert_mu + ins_size_mle)**2 for p in cluster])
    lr = lr / (2 * insert_sigma**2)

    if conditioning:
        n = len(cluster)
        lr += n * (np.log(normcdf(cutoff, insert_mu, insert_sigma))
                   - np.log(normcdf(cutoff + ins_size_mle, insert_mu, insert_sigma)))

    return lr


# no cutoff or conditioning
def lr_inv(cluster, insert_mu, insert_sigma, cutoff=None, conditioning=None):
    return len(cluster)


# no cutoff or conditioning
def lr_dup(cluster, insert_mu, insert_sigma, cutoff=None, conditioning=None):
    return len(cluster)


lr_fun = {'Del': lr_del, 'Ins': lr_ins, 'InvL': lr_inv, 'InvR': lr_inv, 'Dup': lr_dup}


# does opts['pecluster_null_reps'] replicates of null cluster simulation and returns
# a sorted list of the null cluster likelihood ratios
# returns sorted list of null likelihood ratios under a permutation simulation
def compute_null_dist(opts, discordant_pairs, dtype,
                      insert_mu, insert_sigma,
                      gap_file, lib_idx, lr_cond):
    nreps = opts['pecluster_null_reps']
    chrom_name, start, end = opts['chromosome'], opts['region_start'], opts['region_end']
    gaps_inter = load_genome_gaps(gap_file, chrom_name)
    chrom_inter = pyinter.IntervalSet()
    chrom_inter.add(pyinter.closedopen(start, end))
    non_gaps_inter = chrom_inter.difference(gaps_inter)
    non_gaps = [(i.lower_value, i.upper_value) for i in non_gaps_inter]
    total_len = sum([i[1] - i[0] for i in non_gaps])

    # For deletion null clusters, don't use pairs that are obviously too large.
    # (for normal data the discordant read cutoff for deletion supports
    #  is like mu + 3 sigma ~ mu + .3mu, and we're excluding stuff bigger than 3mu)
    if dtype == 'Del':
        max_null_insert = insert_mu * opts['insert_max_mu_multiple']
    else:
        max_null_insert = np.Inf

    null_clusters = []
    lr_null_clusters = np.array([], float)
    for _ in range(nreps):
        shuffled = shuffle_discordant_pairs(discordant_pairs, total_len,
                                            max_insert_size=max_null_insert)
        clusters_tmp, _ = cluster_pairs(opts, shuffled, dtype, lib_idx,
                                        insert_mu, insert_sigma)
        null_clusters.extend(clusters_tmp)
        lr_tmp = np.fromiter((lr_fun[dtype](c, insert_mu, insert_sigma,
                                            opts['insert_cutoff'], lr_cond)
                              for c in clusters_tmp),
                             float)
        lr_null_clusters = np.append(lr_null_clusters, lr_tmp)
    if opts['verbosity'] > 1:
        print('[compute_null_dist] {0}'.format(dtype))
        print('shuffled lr:')
        print(lr_null_clusters)
        print('')

    outname = ('{0}_{1}_null_cluster_{2}reps.txt'
               .format(opts['library_names'][lib_idx], dtype, nreps))
    fname = os.path.join(opts['outdir'], 'logging', outname)
    write_clustering_results(fname, list(zip(lr_null_clusters, null_clusters)), first_reject=0)

    # print('there were {0} {1} clusters after shuffling'.format(len(clusters),
    #                                                            dtype))

    lr_null_clusters.sort()
    return lr_null_clusters


def fdr_discordant_clusters(opts, clusters, lr_null_clusters, dtype,
                            insert_mu, insert_sigma, insert_cutoff, lr_cond):
    # SPEEDUP this should be computed already
    lr_clusters = [lr_fun[dtype](c, insert_mu, insert_sigma, insert_cutoff, lr_cond)
                   for c in clusters]
    lr_pairs = list(zip(lr_clusters, clusters))
    lr_pairs.sort()

    # find FDR cutoff
    target_fdr = opts['pecluster_target_fdr']
    null_sim_nreps = opts['pecluster_null_reps']
    M = len(lr_clusters)
    M_0 = len(lr_null_clusters) / null_sim_nreps

    # P(null score >= x)
    def p_null_exceeds(x):
        return 1 - bisect_left(lr_null_clusters, x) / len(lr_null_clusters)

    if M > 0:
        pi_0 = min(1, M_0 / M)
    else:
        pi_0 = None

    if M_0 == 0:                # no nulls, reject everything
        j = 0
    else:
        for j in range(M + 1):
            if j == M:              # failed, can't reject anything
                break
            # estimated fdr from rejecting lr_pairs[j:]
            num_rej = M - j
            s_j = lr_pairs[j][0]
            est_fdr = M * pi_0 * p_null_exceeds(s_j) / num_rej
            if est_fdr <= target_fdr:
                break

    clusters_pass = [lr_pairs[i][1] for i in range(j, M)]
    clusters_fail = [lr_pairs[i][1] for i in range(0, j)]

    return clusters_pass, clusters_fail, lr_pairs, j


def write_clustering_results(filename, lr_pairs, first_reject):
    fout = open(filename, 'w')
    if len(lr_pairs) > 0:
        lr_clusters, clusters = list(zip(*lr_pairs))
        for i in range(len(clusters)):
            npairs = len(clusters[i])
            pos1 = [p.pos1 for p in clusters[i]]
            pos2 = [p.pos2 for p in clusters[i]]
            lr = lr_clusters[i]
            passing = True if i >= first_reject else False
            qnames = ';'.join([p.qname for p in clusters[i]])
            fout.write('{0}\t{1}\t{2}\t{3}\t{4}\t{5}\t{6}\t{7}\n'
                       .format(min(pos1), max(pos1), min(pos2), max(pos2), npairs,
                               lr, passing, qnames))
    fout.close()


def cluster_to_bp(cluster, confidence, dtype, libname):
    left_ci, right_ci = cluster_to_ci(cluster, confidence, dtype)
    pe = [(p.qname, dtype) for p in cluster]
    libs = ['pe_' + libname] * len(cluster)
    left_bp = Breakpoint(left_ci, pe=pe, libs=libs)
    right_bp = Breakpoint(right_ci, pe=pe, libs=libs)
    return left_bp, right_bp


def cluster_to_ci(cluster, confidence, dtype):
    p1 = [p.pos1 for p in cluster]
    p2 = [p.pos2 for p in cluster]
    lower_1, upper_1 = uniform_ci(p1, confidence)
    lower_2, upper_2 = uniform_ci(p2, confidence)
    if dtype == 'Del':
        return upper_1, lower_2
    elif dtype == 'Ins':
        return upper_1, lower_2
    elif dtype == 'Dup':
        return lower_1, upper_2
    elif dtype == 'InvL':
        return upper_1, upper_2
    elif dtype == 'InvR':
        return lower_1, lower_2
