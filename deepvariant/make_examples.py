# Copyright 2020 Google LLC.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its
#    contributors may be used to endorse or promote products derived from this
#    software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
"""Step one of DeepVariant: creates tf.Example protos for training/calling."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import sys
if 'google' in sys.modules and 'google.protobuf' not in sys.modules:
  del sys.modules['google']


import collections
import re
import time


from absl import app
from absl import flags
from absl import logging
import numpy as np
import tensorflow as tf

from deepvariant import dv_constants
from deepvariant import exclude_contigs
from deepvariant import logging_level
from deepvariant import make_examples_utils
from deepvariant import pileup_image
from deepvariant import resources
from deepvariant import tf_utils
from deepvariant import vcf_candidate_importer
from deepvariant import very_sensitive_caller
from deepvariant.labeler import customized_classes_labeler
from deepvariant.labeler import haplotype_labeler
from deepvariant.labeler import positional_labeler
from deepvariant.protos import deepvariant_pb2
from deepvariant.python import allelecounter
from deepvariant.realigner import realigner
from deepvariant.vendor import timer
from google.protobuf import text_format
from third_party.nucleus.io import fasta
from third_party.nucleus.io import sam
from third_party.nucleus.io import sharded_file_utils
from third_party.nucleus.io import tfrecord
from third_party.nucleus.io import vcf
from third_party.nucleus.io.python import hts_verbose
from third_party.nucleus.protos import reads_pb2
from third_party.nucleus.util import errors
from third_party.nucleus.util import proto_utils
from third_party.nucleus.util import ranges
from third_party.nucleus.util import utils
from third_party.nucleus.util import variant_utils

FLAGS = flags.FLAGS

# Sentinel command line flag value indicating no downsampling should occur.
NO_DOWNSAMPLING = 0.0

# Sentinel command line flag value indicating no random ref sites should be
# emitted.
NO_RANDOM_REF = 0.0

# The name used for a sample if one is not specified or present in the reads.
_UNKNOWN_SAMPLE = 'UNKNOWN'

# The extension we add to our examples path to write our MakeExamplesRunInfo
# protobuf.
_RUN_INFO_FILE_EXTENSION = '.run_info.pbtxt'

# Use a default hts_block_size value of 128 MB (see internal for details) to
# improve SAM/BAM reading throughput, particularly on remote filesystems. Do not
# modify this default parameter without a systematic evaluation of the impact
# across a variety of distributed filesystems!
_DEFAULT_HTS_BLOCK_SIZE = 128 * (1024 * 1024)

# For --runtime_by_region, these columns will be written out in this order.
RUNTIME_BY_REGION_COLUMNS = ('region', 'get reads', 'find candidates',
                             'make pileup images', 'write outputs', 'num reads',
                             'num candidates', 'num examples')

flags.DEFINE_string(
    'ref', None,
    'Required. Genome reference to use. Must have an associated FAI index as '
    'well. Supports text or gzipped references. Should match the reference '
    'used to align the BAM file provided to --reads.')
flags.DEFINE_string(
    'reads', None,
    'Required. Aligned, sorted, indexed BAM file containing the reads we want '
    'to call. Should be aligned to a reference genome compatible with --ref. '
    'Can provide multiple BAMs (comma-separated).')
flags.DEFINE_bool(
    'use_ref_for_cram', True,
    'If true, use the --ref argument as the reference file for the CRAM '
    'file passed to --reads.  In this case, it is required that the reference '
    'file be located on a local POSIX filesystem. To disable, specify '
    '--nouse_ref_for_cram.')
flags.DEFINE_string(
    'examples', None,
    'Required. Path to write tf.Example protos in TFRecord format.')
flags.DEFINE_string(
    'candidates', '',
    'Candidate DeepVariantCalls in tfrecord format. For DEBUGGING.')
flags.DEFINE_string('mode', None,
                    'Mode to run. Must be one of calling or training')
flags.DEFINE_string(
    'regions', '',
    'Optional. Space-separated list of regions we want to process. Elements '
    'can be region literals (e.g., chr20:10-20) or paths to BED/BEDPE files.')
flags.DEFINE_string(
    'exclude_regions', '',
    'Optional. Space-separated list of regions we want to exclude from '
    'processing. Elements can be region literals (e.g., chr20:10-20) or paths '
    'to BED/BEDPE files. Region exclusion happens after processing the '
    '--regions argument, so --region 20 --exclude_regions 20:100 does '
    'everything on chromosome 20 excluding base 100')
flags.DEFINE_string(
    'variant_caller', 'very_sensitive_caller',
    'The caller to use to make examples. Must be one of the VariantCaller enum '
    'values in the DeepVariantOptions proto.')
flags.DEFINE_string(
    'gvcf', '',
    'Optional. Path where we should write gVCF records in TFRecord of Variant '
    'proto format.')
flags.DEFINE_integer(
    'gvcf_gq_binsize', 5,
    'Bin size in which to quantize gVCF genotype qualities. Larger bin size '
    'reduces the number of gVCF records at a loss of quality granularity.')
flags.DEFINE_string(
    'confident_regions', '',
    'Regions that we are confident are hom-ref or a variant in BED format. In '
    'BED or other equivalent format, sorted or unsorted. Contig names must '
    'match those of the reference genome.')
flags.DEFINE_string(
    'truth_variants', '',
    'Tabix-indexed VCF file containing the truth variant calls for this labels '
    'which we use to label our examples.')
flags.DEFINE_string(
    'proposed_variants', '',
    '(Only used when --variant_caller=vcf_candidate_importer.) '
    'Tabix-indexed VCF file containing the proposed positions and alts for '
    '`vcf_candidate_importer`. The GTs will be ignored.')
flags.DEFINE_integer('task', 0, 'Task ID of this task')
flags.DEFINE_integer(
    'partition_size', 1000,
    'The maximum number of basepairs we will allow in a region before splitting'
    'it into multiple smaller subregions.')
flags.DEFINE_integer(
    'max_reads_per_partition', 1500,
    'The maximum number of reads per partition that we consider before '
    'following processing such as sampling and realigner.')
flags.DEFINE_string(
    'multi_allelic_mode', '',
    'How to handle multi-allelic candidate variants. For DEBUGGING')
flags.DEFINE_bool(
    'realign_reads', True,
    'If True, locally realign reads before calling variants. '
    'Reads longer than 500 bp are never realigned.')
flags.DEFINE_bool(
    'write_run_info', False,
    'If True, write out a MakeExamplesRunInfo proto besides our examples in '
    'text_format.')
flags.DEFINE_enum(
    'alt_aligned_pileup', 'none',
    ['none', 'base_channels', 'diff_channels', 'rows'],
    'Include alignments of reads against each candidate alternate allele in '
    'the pileup image. "none" turns this feature off. '
    'The default is "none".'
    'Options: "none", "base_channels","diff_channels", "rows"')
flags.DEFINE_enum(
    'types_to_alt_align', 'indels', ['indels', 'all'],
    'When --alt_aligned_pileup is not none, this flag determines whether to '
    'align to the alt alleles only for indels or for all variant types '
    'including SNPs. Ignored if --alt_aligned_pileup is "none". This flag is '
    'experimental and is not compatible with the pre-trained release models.')
flags.DEFINE_float(
    'downsample_fraction', NO_DOWNSAMPLING,
    'If not ' + str(NO_DOWNSAMPLING) + ' must be a value between 0.0 and 1.0. '
    'Reads will be kept (randomly) with a probability of downsample_fraction '
    'from the input BAM. This argument makes it easy to create examples as '
    'though the input BAM had less coverage.')
flags.DEFINE_string(
    'sample_name', '', 'Sample name to use for our sample_name in the output '
    'Variant/DeepVariantCall protos. If not specified, will be inferred from '
    'the header information from --reads.')
flags.DEFINE_string('hts_logging_level',
                    hts_verbose.htsLogLevel.HTS_LOG_WARNING.name,
                    'Sets the htslib logging threshold.')
flags.DEFINE_integer(
    'hts_block_size', _DEFAULT_HTS_BLOCK_SIZE,
    'Sets the htslib block size. Zero or negative uses default htslib setting; '
    'larger values (e.g. 1M) may be beneficial for using remote files. '
    'Currently only applies to SAM/BAM reading.')
flags.DEFINE_integer(
    'min_base_quality', 10,
    'Minimum base quality. This field indicates that we are enforcing a '
    'minimum base quality score for alternate alleles. Alternate alleles will '
    'only be considered if all bases in the allele have a quality greater than '
    'min_base_quality.')
flags.DEFINE_integer(
    'min_mapping_quality', 5,
    'By default, reads with any mapping quality are kept. Setting this field '
    'to a positive integer i will only keep reads that have a MAPQ >= i. Note '
    'this only applies to aligned reads.')
flags.DEFINE_integer(
    'vsc_min_count_snps', 2,
    'SNP alleles occurring at least this many times in our '
    'AlleleCount will be advanced as candidates.')
flags.DEFINE_integer(
    'vsc_min_count_indels', 2,
    'Indel alleles occurring at least this many times in '
    'our AlleleCount will be advanced as candidates.')
flags.DEFINE_float(
    'vsc_min_fraction_snps', 0.12,
    'SNP alleles occurring at least this fraction of all '
    'counts in our AlleleCount will be advanced as '
    'candidates.')
flags.DEFINE_float(
    'vsc_min_fraction_indels', 0.06,
    'Indel alleles occurring at least this fraction of all counts in our '
    'AlleleCount will be advanced as candidates.')
flags.DEFINE_float(
    'training_random_emit_ref_sites', NO_RANDOM_REF,
    'If > 0, emit extra random reference examples with this probability.')
flags.DEFINE_integer(
    'pileup_image_height', 0,
    'Height for the pileup image. If 0, uses the default height')
flags.DEFINE_integer(
    'pileup_image_width', 0,
    'Width for the pileup image. If 0, uses the default width')
flags.DEFINE_string(
    'labeler_algorithm', 'haplotype_labeler',
    'Algorithm to use to label examples in training mode. Must be one of the '
    'LabelerAlgorithm enum values in the DeepVariantOptions proto.')
flags.DEFINE_string(
    'customized_classes_labeler_classes_list', '',
    'A comma-separated list of strings that defines customized class labels '
    'for variants. This is only set when labeler_algorithm is '
    'customized_classes_labeler.')
flags.DEFINE_string(
    'customized_classes_labeler_info_field_name', '',
    'The name from the INFO field of VCF where we should get the customized '
    'class labels from. This is only set when labeler_algorithm is '
    'customized_classes_labeler.')
flags.DEFINE_integer(
    'logging_every_n_candidates', 100,
    'Print out the log every n candidates. The smaller the number, the more '
    'frequent the logging information emits.')
flags.DEFINE_bool('keep_duplicates', False, 'If True, keep duplicate reads.')
flags.DEFINE_bool('keep_supplementary_alignments', False,
                  'If True, keep reads marked as supplementary alignments.')
flags.DEFINE_bool('keep_secondary_alignments', False,
                  'If True, keep reads marked as secondary alignments.')
flags.DEFINE_bool(
    'parse_sam_aux_fields', None,
    'If True, auxiliary fields of the SAM/BAM/CRAM records are parsed. '
    'By default this flag is None. This flag will be automatically turned on '
    'if other flags need it (e.g., sort_by_haplotypes). '
    'If it is explicitly set by the user (either True or False), the '
    'user-specified value will be used.')
flags.DEFINE_bool('use_original_quality_scores', False,
                  'If True, base quality scores are read from OQ tag.')
flags.DEFINE_string(
    'select_variant_types', None,
    'If provided, should be a whitespace-separated string of variant types to '
    'keep when generating examples. Permitted values are "snps", "indels", '
    '"multi-allelics", and "all", which select bi-allelic snps, bi-allelic '
    'indels, multi-allelic variants of any type, and all variants, '
    'respectively. Multiple selectors can be specified, so that '
    '--select_variant_types="snps indels" would keep all bi-allelic SNPs and '
    'indels')
flags.DEFINE_string(
    'sequencing_type', None,
    'A string representing input bam file sequencing_type. Permitted values are '
    '"WGS" and "WES", which represent whole genome sequencing and whole exome '
    'sequencing, respectively. This flag is experimental and is not currently '
    'being used.')
flags.DEFINE_bool(
    'sort_by_haplotypes', False,
    'If True, reads are sorted by haplotypes (using HP tag), '
    'parse_sam_aux_fields has to be set for this to work.')
flags.DEFINE_integer(
    'sort_by_haplotypes_sample_hp_tag', 0,
    'If set to > 0, reads with this HP tag will be sorted on top. '
    'sort_by_haplotypes has to be set to True for this to work.')
flags.DEFINE_bool(
    'add_hp_channel', False,
    'If true, add another channel to represent HP tags per read.')
flags.DEFINE_bool(
    'add_supporting_other_alt_color', False,
    'If True, reads supporting an alt not represented in the '
    'pileup image are colored differently for multiallelics.')
flags.DEFINE_string(
    'population_vcfs', None,
    'Optional. Tabix-indexed VCF file (or list of VCFs broken by chromosome),'
    ' separated by comma or space, '
    'containing population allele frequencies.')
flags.DEFINE_bool(
    'use_allele_frequency', False,
    'If True, add another channel for pileup images to represent allele '
    'frequency information gathered from population callsets.')
flags.DEFINE_string(
    'runtime_by_region', None,
    '[optional] Output filename for a TSV file of runtimes and '
    'other stats by region. If examples are sharded, this should be sharded '
    'into the same number of shards as the examples.')

# ---------------------------------------------------------------------------
# Selecting variants of specific types (e.g., SNPs)
# ---------------------------------------------------------------------------


def _select_biallelic_snps(v):
  return variant_utils.is_snp(v) and variant_utils.is_biallelic(v)


def _select_biallelic_indels(v):
  return variant_utils.is_indel(v) and variant_utils.is_biallelic(v)


def _select_biallelic_insertions(v):
  return variant_utils.has_insertion(v) and variant_utils.is_biallelic(v)


def _select_biallelic_deletions(v):
  return variant_utils.has_deletion(v) and variant_utils.is_biallelic(v)


_VARIANT_TYPE_SELECTORS = {
    'snps': _select_biallelic_snps,
    'indels': _select_biallelic_indels,
    'insertions': _select_biallelic_insertions,
    'deletions': _select_biallelic_deletions,
    'multi-allelics': variant_utils.is_multiallelic,
    'all': lambda v: True,
}

# ---------------------------------------------------------------------------
# Option handling
# ---------------------------------------------------------------------------


def parse_proto_enum_flag(proto_enum_pb2,
                          flag_value,
                          skip_unspecified_option=True):
  """Parses a command line flag string value into a protobuf Enum value.

  Args:
    proto_enum_pb2: a enum_type_wrapper.EnumTypeWrapper type containing a proto
      enum definition. For example, this would be
      deepvariant_pb2.DeepVariantOptions.Mode to get the DeepVariantOptions Mode
      enum. See:
      https://developers.google.com/protocol-buffers/docs/reference/python-generated#enum
        for more information.
    flag_value: str. The name of the proto enum option from the command line we
      want to convert into the enum value.
    skip_unspecified_option: bool. If True, any enum options that include the
      string 'unspecified' (in any case) will be excluded from the list of
      allowed options in the ValueError raised if flag_value isn't valid.

  Returns:
    The enum value for flag_value in proto_enum_pb2

  Raises:
    ValueError: if flag_value isn't a valid enum name in proto_enum_pb2.
  """
  try:
    return proto_enum_pb2.Value(flag_value)
  except ValueError:
    options = proto_enum_pb2.keys()
    if skip_unspecified_option:
      options = [o for o in options if 'unspecified' not in o.lower()]
    raise ValueError('Unknown enum option "{}". Allowed options are {}'.format(
        flag_value, ','.join(sorted(options))))


def set_parse_sam_aux_fields(flags_obj, flag_names):
  for flag_name in flag_names:
    if flags_obj[flag_name].value:
      logging.warning(
          'Because --%s=true, --parse_sam_aux_fields is set to '
          'true to enable reading auxiliary fields from reads.', flag_name)
    return True
  return False


def parse_regions_flag(regions_flag_value):
  if isinstance(regions_flag_value, str):
    regions_flag_value = regions_flag_value.split()
  return regions_flag_value


def default_options(add_flags=True, flags_obj=None):
  """Creates a DeepVariantOptions proto populated with reasonable defaults.

  Args:
    add_flags: bool. defaults to True. If True, we will push the value of
      certain FLAGS into our options. If False, those option fields are left
      uninitialized.
    flags_obj: object.  If not None, use as the source of flags, else use global
      FLAGS.

  Returns:
    deepvariant_pb2.DeepVariantOptions protobuf.

  Raises:
    ValueError: If we observe invalid flag values.
  """
  if not flags_obj:
    flags_obj = FLAGS

  read_reqs = reads_pb2.ReadRequirements(
      keep_duplicates=flags_obj.keep_duplicates,
      keep_supplementary_alignments=flags_obj.keep_supplementary_alignments,
      keep_secondary_alignments=flags_obj.keep_secondary_alignments,
      min_base_quality=flags_obj.min_base_quality,
      min_mapping_quality=flags_obj.min_mapping_quality,
      min_base_quality_mode=reads_pb2.ReadRequirements.ENFORCED_BY_CLIENT)

  logging.vlog(3, 'ReadRequirements are: %s', read_reqs)

  pic_options = pileup_image.default_options(read_requirements=read_reqs)

  allele_counter_options = deepvariant_pb2.AlleleCounterOptions(
      partition_size=flags_obj.partition_size, read_requirements=read_reqs)

  if flags_obj.sample_name:
    sample_name = flags_obj.sample_name
  elif flags_obj.reads:
    # If there are multiple BAM files, use the sample name from the first one.
    with sam.SamReader(flags_obj.reads.split(',')[0]) as sam_reader:
      sample_name = extract_sample_name_from_sam_reader(sam_reader)
  else:
    sample_name = _UNKNOWN_SAMPLE

  variant_caller_options = deepvariant_pb2.VariantCallerOptions(
      min_count_snps=flags_obj.vsc_min_count_snps,
      min_count_indels=flags_obj.vsc_min_count_indels,
      min_fraction_snps=flags_obj.vsc_min_fraction_snps,
      min_fraction_indels=flags_obj.vsc_min_fraction_indels,
      # Not specified by default: fraction_reference_sites_to_emit,
      # Fixed random seed produced with 'od -vAn -N4 -tu4 < /dev/urandom'.
      random_seed=1400605801,
      sample_name=sample_name,
      p_error=0.001,
      max_gq=50,
      gq_resolution=flags_obj.gvcf_gq_binsize,
      ploidy=2)

  options = deepvariant_pb2.DeepVariantOptions(
      exclude_contigs=exclude_contigs.EXCLUDED_HUMAN_CONTIGS,
      # Fixed random seed produced with 'od -vAn -N4 -tu4 < /dev/urandom'.
      random_seed=609314161,
      # # Not specified by default: calling_regions = 3;
      read_requirements=read_reqs,
      allele_counter_options=allele_counter_options,
      variant_caller_options=variant_caller_options,
      pic_options=pic_options,
      n_cores=1,
      task_id=0,
      num_shards=0,
      min_shared_contigs_basepairs=0.9,
  )

  if add_flags:
    options.mode = parse_proto_enum_flag(
        deepvariant_pb2.DeepVariantOptions.Mode, flags_obj.mode.upper())

    options.labeler_algorithm = parse_proto_enum_flag(
        deepvariant_pb2.DeepVariantOptions.LabelerAlgorithm,
        flags_obj.labeler_algorithm.upper())

    options.variant_caller = parse_proto_enum_flag(
        deepvariant_pb2.DeepVariantOptions.VariantCaller,
        flags_obj.variant_caller.upper())

    if flags_obj.ref:
      options.reference_filename = flags_obj.ref
    if flags_obj.reads:
      options.reads_filenames.extend(flags_obj.reads.split(','))
    if flags_obj.confident_regions:
      options.confident_regions_filename = flags_obj.confident_regions
    if flags_obj.truth_variants:
      options.truth_variants_filename = flags_obj.truth_variants
    if flags_obj.proposed_variants:
      options.proposed_variants_filename = flags_obj.proposed_variants
    if flags_obj.sequencing_type:
      options.pic_options.sequencing_type = parse_proto_enum_flag(
          deepvariant_pb2.PileupImageOptions.SequencingType,
          flags_obj.sequencing_type)
    if flags_obj.downsample_fraction != NO_DOWNSAMPLING:
      options.downsample_fraction = flags_obj.downsample_fraction

    if flags_obj.multi_allelic_mode:
      multi_allelic_enum = {
          'include_het_alt_images':
              deepvariant_pb2.PileupImageOptions.ADD_HET_ALT_IMAGES,
          'exclude_het_alt_images':
              deepvariant_pb2.PileupImageOptions.NO_HET_ALT_IMAGES,
      }[flags_obj.multi_allelic_mode]
      options.pic_options.multi_allelic_mode = multi_allelic_enum

    if flags_obj.pileup_image_height:
      options.pic_options.height = flags_obj.pileup_image_height
    if flags_obj.pileup_image_width:
      options.pic_options.width = flags_obj.pileup_image_width

    options.pic_options.alt_aligned_pileup = flags_obj.alt_aligned_pileup
    options.pic_options.types_to_alt_align = flags_obj.types_to_alt_align
    if flags_obj.add_supporting_other_alt_color:
      options.pic_options.other_allele_supporting_read_alpha = 0.3

    if flags_obj.select_variant_types:
      options.select_variant_types[:] = flags_obj.select_variant_types.split()
      for svt in options.select_variant_types:
        if svt not in _VARIANT_TYPE_SELECTORS:
          errors.log_and_raise(
              'Select variant type {} not recognized. Allowed values are {}'
              .format(svt, ', '.join(_VARIANT_TYPE_SELECTORS)),
              errors.CommandLineError)

    num_shards, examples, candidates, gvcf, runtime_by_region = (
        sharded_file_utils.resolve_filespecs(flags_obj.task,
                                             flags_obj.examples or '',
                                             flags_obj.candidates or '',
                                             flags_obj.gvcf or '',
                                             flags_obj.runtime_by_region or ''))
    options.examples_filename = examples
    options.candidates_filename = candidates
    options.gvcf_filename = gvcf
    options.task_id = flags_obj.task
    options.num_shards = num_shards
    options.runtime_by_region = runtime_by_region

    # First, if parse_sam_aux_fields is still None (which means the users didn't
    # set it. We added some logic to decide its value.
    if flags_obj.parse_sam_aux_fields is None:
      flags_that_needs_sam_aux_fields = [
          'add_hp_channel', 'sort_by_haplotypes', 'use_original_quality_scores'
      ]
      flags_obj.parse_sam_aux_fields = set_parse_sam_aux_fields(
          flags_obj, flags_that_needs_sam_aux_fields)

    for flags_strictly_needs_sam_aux_fields in [
        'sort_by_haplotypes', 'use_original_quality_scores'
    ]:
      if (flags_obj[flags_strictly_needs_sam_aux_fields].value and
          not flags_obj.parse_sam_aux_fields):
        errors.log_and_raise(
            'If --{} is set then parse_sam_aux_fields '
            'must be set too.'.format(flags_strictly_needs_sam_aux_fields),
            errors.CommandLineError)
    options.use_original_quality_scores = flags_obj.use_original_quality_scores

    for flag_optionally_needs_sam_aux_fields in ['add_hp_channel']:
      if (flags_obj[flag_optionally_needs_sam_aux_fields].value and
          not flags_obj.parse_sam_aux_fields):
        logging.warning(
            'WARGNING! --%s is set but --parse_sam_aux_fields is not '
            'set. This will cause aux fields to not be read in. The relevant '
            'values might be zero. For example, for --add_hp_channel, '
            'resulting in an empty HP channel. If this is not what you '
            'intended, please stop and enable --parse_sam_aux_fields.',
            flag_optionally_needs_sam_aux_fields)
    if flags_obj.add_hp_channel:
      options.pic_options.num_channels += 1
      options.pic_options.add_hp_channel = True

    if flags_obj.sort_by_haplotypes_sample_hp_tag < 0:
      errors.log_and_raise(
          '--sort_by_haplotypes_sample_hp_tag has to be set to a positive int.',
          errors.CommandLineError)
    if (flags_obj.sort_by_haplotypes_sample_hp_tag > 0 and
        not flags_obj.sort_by_haplotypes):
      errors.log_and_raise(
          '--sort_by_haplotypes_sample_hp_tag requires --sort_by_haplotypes to be '
          'set ', errors.CommandLineError)
    if flags_obj.sort_by_haplotypes and not flags_obj.parse_sam_aux_fields:
      errors.log_and_raise(
          '--sort_by_haplotypes requires --parse_sam_aux_fields to be set ',
          errors.CommandLineError)
    options.pic_options.sort_by_haplotypes = flags_obj.sort_by_haplotypes
    options.pic_options.sort_by_haplotypes_sample_hp_tag = flags_obj.sort_by_haplotypes_sample_hp_tag

    if flags_obj.write_run_info:
      options.run_info_filename = examples + _RUN_INFO_FILE_EXTENSION

    options.calling_regions.extend(parse_regions_flag(flags_obj.regions))
    options.exclude_calling_regions.extend(
        parse_regions_flag(flags_obj.exclude_regions))

    options.realigner_enabled = flags_obj.realign_reads
    options.realigner_options.CopyFrom(realigner.realigner_config(flags_obj))

    options.max_reads_per_partition = flags_obj.max_reads_per_partition

    if (options.mode == deepvariant_pb2.DeepVariantOptions.TRAINING and
        flags_obj.training_random_emit_ref_sites != NO_RANDOM_REF):
      options.variant_caller_options.fraction_reference_sites_to_emit = (
          flags_obj.training_random_emit_ref_sites)

    if (flags_obj.use_allele_frequency and not flags_obj.population_vcfs):
      errors.log_and_raise(
          'If use_allele_frequency is set then population_vcfs '
          'must be provided.', errors.CommandLineError)
    if flags_obj.use_allele_frequency:
      options.use_allele_frequency = flags_obj.use_allele_frequency
      options.pic_options.num_channels += 1
      options.pic_options.use_allele_frequency = True
    if flags_obj.population_vcfs:
      options.population_vcf_filenames.extend(
          re.split(',| ', flags_obj.population_vcfs))

  return options


def logging_with_options(options, message):
  """If options contain multiple shards, log with task/shard prefix."""
  if options.num_shards > 1:
    prefix = 'Task {}/{}: '.format(options.task_id, options.num_shards)
  else:
    prefix = ''
  logging.info('%s%s', prefix, message)


# ---------------------------------------------------------------------------
# Simple utilities
# ---------------------------------------------------------------------------


def in_training_mode(options):
  return options.mode == deepvariant_pb2.DeepVariantOptions.TRAINING


def gvcf_output_enabled(options):
  """Returns True if we should be generating gVCF output."""
  return bool(options.gvcf_filename)


def only_true(*elts):
  """Returns the sublist of elements that evaluate to True."""
  return [elt for elt in elts if elt]


def extract_sample_name_from_sam_reader(sam_reader):
  """Returns the sample name as derived from the BAM file of reads.

  Args:
    sam_reader: Already opened sam_reader to use to extract the sample names
      from. This sam_reader will not be closed after this function returns.

  Returns:
    The sample ID annotated in the read group.

  Raises:
    ValueError: There is not exactly one unique sample name in the SAM/BAM.
  """
  samples_list = [
      rg.sample_id for rg in sam_reader.header.read_groups if rg.sample_id
  ]
  samples = set(samples_list)
  if not samples:
    logging.warning(
        'No non-empty sample name found in the input reads. '
        'DeepVariant will use %s as the sample name. You can also '
        'provide a sample name with the --sample_name argument.',
        dv_constants.DEFAULT_SAMPLE_NAME)
    return dv_constants.DEFAULT_SAMPLE_NAME
  elif len(samples) > 1:
    logging.warning(
        'Multiple samples (%s) were found in the input reads. '
        'Please confirm this is intended. For now, DeepVariant '
        'will use the first sample name %s.', ', '.join(sorted(samples)),
        samples_list[0])
    return samples_list[0]
  return next(iter(samples))


def trim_runtime(seconds: float) -> float:
  """Round seconds (float) to the nearest millisecond."""
  return round(seconds, 3)


# ---------------------------------------------------------------------------
# Utilities for working with labeling metrics
#
# ---------------------------------------------------------------------------


def read_make_examples_run_info(path):
  """Reads a MakeExamplesRunInfo proto in text_format from path."""
  with tf.io.gfile.GFile(path) as f:
    return text_format.Parse(f.read(), deepvariant_pb2.MakeExamplesRunInfo())


def write_make_examples_run_info(run_info_proto, path):
  """Writes a MakeExamplesRunInfo proto in text_format to path."""
  with tf.io.gfile.GFile(path, mode='w') as writer:
    writer.write(text_format.MessageToString(run_info_proto, float_format=''))


# ---------------------------------------------------------------------------
# Utilities for variant matching
#
# ---------------------------------------------------------------------------
def get_allele_frequency(variant, index):
  """Gets allele frequency of the index-th alt_base of a Variant proto.

  Args:
    variant: A Variant proto.
    index: The index where we want to query allele frequency.

  Returns:
    A float. The queried allele frequency.

  Raises:
    ValueError: If the Variant proto does not include 'AF' field or the 'AF'
      field encounters an out-of-bound error when querying at position `index`
  """
  if variant.info.get('AF'):
    if index < len(variant.info['AF'].values):
      return variant.info['AF'].values[index].number_value
    else:
      raise ValueError('Invalid index', index, 'for the info[AF] field',
                       variant.info['AF'].values)
  raise ValueError('Variant does not have an AF field')


def get_ref_allele_frequency(variant):
  """Gets REF allele frequency for a Variant proto."""
  sum_alt_frequency = 0
  for alt_idx, _ in enumerate(variant.alternate_bases):
    sum_alt_frequency += get_allele_frequency(variant, alt_idx)
  return 1 - sum_alt_frequency


def get_ref_haplotype_and_offset(dv_variant, cohort_variants, ref_reader):
  """Gets reference haplotype that overlaps with given variants and its offset.

  Reference offset is the starting position of the returned haplotype.
  It is the minimal starting positions of dv_variant and cohort_variants.
  Reference haplotype is the reference sequence from reference_offset to the
  maximal ending position of dv_variant and cohort_variants.

  Args:
    dv_variant: A Variant proto.
    cohort_variants: A list of Variant protos.
    ref_reader: A fasta.IndexedFastaReader.

  Returns:
    A tuple of a string and an integer.
      - String: Reference haplotype overlapping with dv_variant and
                cohort_variants.
      - Integer: Offset of the reference haplotype.

  Raises:
    ValueError: If the queried region is invalid.
  """
  # Get the range for haplotypes to be compared.
  min_start_position = min(dv_variant.start,
                           min([cv.start for cv in cohort_variants]))
  max_end_position = max(dv_variant.end,
                         max([cv.end for cv in cohort_variants]))

  haplotype_region = ranges.make_range(dv_variant.reference_name,
                                       min_start_position, max_end_position)

  if ref_reader.is_valid(haplotype_region):
    return ref_reader.query(haplotype_region), min_start_position
  else:
    raise ValueError('Invalid IndexedFastaReader region', haplotype_region)


def update_haplotype(variant, reference_haplotype, reference_offset):
  """Updates haplotypes for a variant.

  A list of variant haplotypes are updated given a variant and a reference
  haplotype (this consists of a sequence and an offset wrt to the reference).
  All ALT alleles are updated as independent updated haplotypes.

  Args:
    variant: A Variant proto.
    reference_haplotype: A string extracted from the reference genome.
    reference_offset: An integer. The offset of the starting position of
      reference_haplotype on reference.

  Raises:
    ValueError: Variant.start is smaller than reference_offset.

  Returns:
    A list of haplotype objects. Haplotype objects are stored as dicts:
      {'haplotype': a haplotype (string),
       'alt': an alt allele (string),
       'variant': an Variant proto}
  """
  if variant.start < reference_offset:
    raise ValueError('The starting position of a variant is smaller than its ',
                     'corresponding reference offset', variant.start,
                     reference_offset)
  offset_start = variant.start - reference_offset
  offset_suffix = \
      variant.start + len(variant.reference_bases) - reference_offset
  list_updated_haplotype = []
  for biallelic_variant in variant.alternate_bases:
    updated_haplotype = reference_haplotype[: offset_start] + \
        biallelic_variant + reference_haplotype[offset_suffix: ]
    dict_haplotype = {
        'haplotype': updated_haplotype,
        'alt': biallelic_variant,
        'variant': variant
    }
    list_updated_haplotype.append(dict_haplotype)

  return list_updated_haplotype


def match_candidate_and_cohort_haplotypes(candidate_haps,
                                          cohort_haps_and_freqs):
  """Match candidate haplotypes with cohort haplotypes and update frequency.

  First, we look for exact haplotype matches between candidate and cohorts.
  If there're any matches, the REF allele frequency associated with the matching
  ALT allele is updated as well.

  Second, if no matches are found, we try to find inexact matches, where only
  REF alleles are matched. The inexact matching step is only used to update REF
  allele frequency. If no exact and inexact matches are found, set REF allele
  frequency to 1.

  Args:
    candidate_haps: A list of haplotype objects from a candidate.
    cohort_haps_and_freqs: A list of haplotype objects from cohorts.
    Haplotype objects are stored as dicts:
      {'haplotype': a haplotype (string),
       'alt': an alt allele (string),
       'variant': an Variant proto}

  Returns:
    A dict with candidate alt alleles as keys, and associated frequencies
    as values.
  """
  dict_allele_frequency = {}
  for candidate_obj in candidate_haps:
    candidate_haplotype = candidate_obj['haplotype']
    candidate_alt = candidate_obj['alt']
    candidate_variant = candidate_obj['variant']

    for cohort_obj in cohort_haps_and_freqs:
      cohort_haplotype = cohort_obj['haplotype']
      # Exact haplotype match.
      if candidate_haplotype == cohort_haplotype:
        cohort_variant = cohort_obj['variant']
        cohort_frequency = get_allele_frequency(
            cohort_variant,
            list(cohort_variant.alternate_bases).index(cohort_obj['alt']))
        dict_allele_frequency[candidate_alt] = cohort_frequency

        # Update REF frequency if it is not in the dictionary.
        if not dict_allele_frequency.get(candidate_variant.reference_bases):
          dict_allele_frequency[candidate_variant.reference_bases] = \
              get_ref_allele_frequency(cohort_variant)

    # For an unmatched alt allele, set the frequency to 0.
    if not dict_allele_frequency.get(candidate_alt):
      dict_allele_frequency[candidate_alt] = 0

  # Calculate REF allele frequency if no exact match was found.
  # It is possible a novel mutation happens at a site where there are other
  # cohort variants. In this case, we cannot simply set REF frequency to 1.
  if sum(dict_allele_frequency.values()) == 0:
    candidate = candidate_haps[0]['variant']
    # Left align variants.
    s_candidate = variant_utils.simplify_variant_alleles(candidate)
    for cohort_obj in cohort_haps_and_freqs:
      s_cohort_variant = variant_utils.simplify_variant_alleles(
          cohort_obj['variant'])
      # Try to find inexact matches to set REF allele frequency.
      # Inexact matches here mean only REF alleles match.
      if s_candidate.start == s_cohort_variant.start and \
          s_candidate.reference_bases == s_cohort_variant.reference_bases:
        dict_allele_frequency[s_candidate.reference_bases] = \
            get_ref_allele_frequency(s_cohort_variant)

    # If still no match, set REF allele frequency to 1.
    if not dict_allele_frequency.get(candidate.reference_bases):
      dict_allele_frequency[candidate.reference_bases] = 1

  return dict_allele_frequency


def find_matching_allele_frequency(variant,
                                   population_vcf_reader,
                                   ref_reader,
                                   padding_bases=0):
  """Finds the allele frequencies of all the alt alleles for a candidate.

  Args:
    variant: A Variant proto generated by make_examples. Note that it can be
      multi-allelic.
    population_vcf_reader: A VcfReader object that reads associated VCF file for
      a candidate. We want to extract allele frequency information in the VCF.
    ref_reader: A IndexedFastaReader object that reads the reference FASTA.
    padding_bases: An integer that specifies the number of padding bases added
      when performing a VCF query. By default it is set to 0.

  Returns:
    A dict with alleles as keys, and allele frequencies as values
  """
  query_region = ranges.make_range(
      chrom=variant.reference_name,
      start=variant.start - padding_bases,
      end=variant.end + padding_bases)
  # Convert to list because we'll look through cohort_variants more than once.
  cohort_variants = list(population_vcf_reader.query(query_region))

  # Init allele frequency dict using alt bases in the candidate.
  dict_allele_frequency = {}
  for alt_base in variant.alternate_bases:
    dict_allele_frequency[alt_base] = 0

  try:
    reference_haplotype, reference_offset = get_ref_haplotype_and_offset(
        variant, cohort_variants, ref_reader)
  except ValueError:
    # If the range associated with variant and cohort_variants is invalid,
    # assume this candidate does not have any ALT alleles.
    dict_allele_frequency = {}
    dict_allele_frequency[variant.reference_bases] = 1
    for alt in variant.alternate_bases:
      dict_allele_frequency[alt] = 0
    return dict_allele_frequency

  candidate_haps = update_haplotype(variant, reference_haplotype,
                                    reference_offset)
  cohort_haps = []
  for cohort_variant in cohort_variants:
    cohort_haps.extend(
        update_haplotype(cohort_variant, reference_haplotype, reference_offset))

  for c in candidate_haps:
    logging.debug('candidate %s, %s', c['haplotype'], c['alt'])
  for c in cohort_haps:
    logging.debug('cohort    %s, %s', c['haplotype'], c['alt'])

  dict_allele_frequency = match_candidate_and_cohort_haplotypes(
      candidate_haps, cohort_haps)

  if dict_allele_frequency:
    logging.vlog(3, '%s:%d-%d, %s > %s', variant.reference_name, variant.start,
                 variant.end, variant.reference_bases, dict_allele_frequency)

  return dict_allele_frequency


# ---------------------------------------------------------------------------
# Region processing
# ---------------------------------------------------------------------------


def _ensure_consistent_contigs(ref_contigs,
                               sam_contigs,
                               vcf_contigs,
                               exclude_contig_names=None,
                               min_coverage_fraction=1.0):
  """Returns the common contigs after ensuring 'enough' overlap.

  Args:
    ref_contigs: list of reference_pb2.ContigInfo protos in the reference
      genome.
    sam_contigs: list of reference_pb2.ContigInfo protos in the SAM/BAM file.
    vcf_contigs: list of reference_pb2.ContigInfo protos in the VCF if in
      training mode, or None otherwise.
    exclude_contig_names: list of strings of contig names to exclude from
      overlap consideration.
    min_coverage_fraction: The fraction of the reference contigs that must be
      shared with all inputs.

  Returns:
    The list of contigs common between all input sources.

  Raises:
    ValueError: The contigs are not sufficiently similar across input sources.
  """
  # Remove any excluded contigs from the ref_contigs, as we want to use the
  # selected contigs for our overlap comparison.
  if exclude_contig_names:
    ref_contigs = [c for c in ref_contigs if c.name not in exclude_contig_names]

  # Compute the common contigs among our inputs, and check that the contigs are
  # sufficiently consistent among each other.
  contigs = common_contigs(only_true(ref_contigs, sam_contigs))
  if vcf_contigs:
    # If VCF contigs exist, we just check the name (not the length).
    vcf_contigs_names = set([x.name for x in vcf_contigs])
    contigs = [x for x in contigs if x.name in vcf_contigs_names]
  validate_reference_contig_coverage(ref_contigs, contigs,
                                     min_coverage_fraction)
  return contigs


def common_contigs(contigs_list):
  """Gets a list of contigs found in all contigs in contigs_list.

  A common contig is considered one where the name and length in basepairs are
  the same.

  Args:
    contigs_list: A sequence of lists of ContigInfo protos.

  Returns:
    A list of ContigInfo protos. Note that the individual protos found in this
    returned list are shared with the ContigInfo protos found in contigs_list,
    so should not be modified.
  """

  def common2(contigs1, contigs2):
    """Computes the common contigs between contigs1 and contigs2."""
    map2 = ranges.contigs_dict(contigs2)

    def is_common(contig1):
      contig2 = map2.get(contig1.name, None)
      return contig2 and contig1.n_bases == contig2.n_bases

    return [c for c in contigs1 if is_common(c)]

  # Compute the common contigs by recursively getting common contigs of our
  # master set of contigs (contigs) and each contig in other_contigs.
  common = contigs_list[0]
  for other_contigs in contigs_list[1:]:
    common = common2(common, other_contigs)

  return common


def validate_reference_contig_coverage(ref_contigs, shared_contigs,
                                       min_coverage_fraction):
  """Validates that shared_contigs spans a sufficient amount of ref_contigs.

  Args:
    ref_contigs: List of ContigInfo protos. All of the contigs from our
      reference genome.
    shared_contigs: The subset of ref_contigs that we found in common with
      ref_contigs and all other genomics data sources.
    min_coverage_fraction: The minimum fraction of basepairs of ref_contigs that
      should be found among the shared_contigs.

  Raises:
    ValueError: If the fraction of covered bases is less than
      min_coverage_fraction.
  """

  def format_contig_matches():
    pieces = []
    common_map = ranges.contigs_dict(shared_contigs)
    for ref_contig in ref_contigs:
      status = 'matched' if ref_contig.name in common_map else 'IS MISSING'
      pieces.append('\n"{}" is {} bp and {}'.format(ref_contig.name,
                                                    ref_contig.n_bases, status))
    return ', '.join(pieces)

  ref_bp = ranges.contigs_n_bases(ref_contigs)
  common_bp = ranges.contigs_n_bases(shared_contigs)
  coverage = common_bp / (1. * ref_bp)
  if not shared_contigs or coverage < min_coverage_fraction:
    raise ValueError('Reference contigs span {} bases but only {} bases '
                     '({:.2%}) were found in common among our input files. '
                     'Check that the sources were created on a common genome '
                     'reference build. Contig matches were: {}. Here is a '
                     'useful article about different human genome reference '
                     'builds:\n'
                     'https://gatkforums.broadinstitute.org/gatk/discussion/'
                     '11010/human-genome-reference-builds-grch38-hg38-b37-hg19'
                     '\nPlease make sure the --ref input matches the build '
                     'used for the input in --reads.'.format(
                         ref_bp, common_bp, coverage, format_contig_matches()))


def build_calling_regions(contigs, regions_to_include, regions_to_exclude):
  """Builds a RangeSet containing the regions we should call variants in.

  This function intersects the Ranges spanning all of the contigs with those
  from regions_to_include, if not empty, and removes all of the regions in
  regions_to_exclude.

  Args:
    contigs: Sequence of ContigInfo protos. Used to determine the initial ranges
      to process (i.e., all bases of these contigs).
    regions_to_include: RangeSet or iterable that can be converted to a
      RangeSet.
    regions_to_exclude: RangeSet or iterable that can be converted to a
      RangeSet.

  Returns:
    A RangeSet.
  """
  # Initially we are going to call everything in the reference.
  regions = ranges.RangeSet.from_contigs(contigs)

  # If we provided a regions to include, intersect it with all of the regions,
  # producing a common set of regions between the reference and the provided
  # calling regions.
  contig_dict = ranges.contigs_dict(contigs)
  if regions_to_include:
    regions = regions.intersection(
        ranges.RangeSet.from_regions(regions_to_include, contig_dict))

  # If we provided regions to exclude, intersect those with the existing calling
  # regions to further refine our set of contigs to process.
  if regions_to_exclude:
    # exclude_regions mutates regions.
    regions.exclude_regions(
        ranges.RangeSet.from_regions(regions_to_exclude, contig_dict))

  return regions


def regions_to_process(contigs,
                       partition_size,
                       calling_regions=None,
                       task_id=None,
                       num_shards=None):
  """Determines the regions to process and partitions them into pieces.

  This function divides the genomes into regions we should process by
  intersecting the Ranges spanning all of the contigs with those from
  calling_regions, if provided. These intersected regions are then partitioned
  into pieces no bigger than partition_size bp in length.

  By construction we ensure that the regions are in genomic order, first w.r.t.
  the contigs and then within each contig by start and end of each region.

  This function can further subdivide these regions into a subset appropriate
  for a single task (task_id) among N tasks (num_shards) to process. The
  function ensures that:

    set(all_regions) = union(regions(task_0), ..., regions(task_n))

  when called with task_ids 0 ... N for num_shards = N.

  Args:
    contigs: Sequence of ContigInfo protos. Used to determine the initial ranges
      to process (i.e., all bases of these contigs) and the order of returned
      ranges.
    partition_size: The maximum size to make any region when partitioning.
    calling_regions: None or RangeSet. If provided, we will intersect the
      regions to process so that only those that overlap a region in this set
      are included.
    task_id: int >= 0 or None. The task_id of this job, which will be used to
      subdivide the total set of regions to process into just those that should
      be processed by this job. Must be < num_shards.
    num_shards: int >= 0 or None. The number of shards (i.e., the total number
      of tasks) we are running in parallel. Together with task_id determines the
      subset of regions we want to process.

  Returns:
    An iterable of nucleus.genomics.v1.Range objects.

  Raises:
    ValueError: if task_id and num_shards are bad or inconsistent.
  """
  if (task_id is None) != (num_shards is None):
    raise ValueError('Both task_id and num_shards must be present if either is',
                     task_id, num_shards)
  if num_shards:
    if num_shards < 0:
      raise ValueError('num_shards={} must be >= 0'.format(num_shards))
    if task_id < 0 or task_id >= num_shards:
      raise ValueError('task_id={} should be >= 0 and < num_shards={}'.format(
          task_id, num_shards))

  regions = ranges.RangeSet.from_contigs(contigs)
  if calling_regions:
    regions = regions.intersection(calling_regions)
  partitioned = regions.partition(partition_size)

  if num_shards:
    return (r for i, r in enumerate(partitioned) if i % num_shards == task_id)
  else:
    return partitioned


def filter_regions_by_vcf(regions, variant_positions):
  """Filter a list of regions to only those that contain variants.

  Args:
    regions: a list of Range objects representing regions to filter on.
    variant_positions: a list of Range objects containing the positions of
      variants.

  Returns:
    filtered_regions: a list of Range objects, each of which appeared in the
        input regions and contains at least one of the input variants.
  """

  def dict_by_chromosome(list_of_ranges):
    d = collections.defaultdict(list)
    for r in list_of_ranges:
      d[r.reference_name].append(r)
    for c in d:
      d[c] = sorted(d[c], key=lambda x: (x.start, x.end))
    return d

  region_dict = dict_by_chromosome(regions)
  variant_dict = dict_by_chromosome(variant_positions)
  filtered_regions = []
  for c in region_dict:
    ri = 0
    vi = 0
    if c not in variant_dict:
      # Skip chromosomes with no variants.
      continue
    while ri < len(region_dict[c]) and vi < len(variant_dict[c]):
      region = region_dict[c][ri]
      variant = variant_dict[c][vi]
      if variant.start >= region.start and variant.start < region.end:
        # When the variant falls within the region, then keep the region.
        filtered_regions.append(region)
        # Move both indices because we're already keeping this region, and we
        # don't need to see any more variants inside this same region.
        ri += 1
        vi += 1
      elif region.start < variant.start:
        # Move past this region since the next variant comes later.
        ri += 1
      else:
        # Found another variant in the previous region we already included.
        vi += 1

  return filtered_regions

# ---------------------------------------------------------------------------
# Region processor
# ---------------------------------------------------------------------------


def read_confident_regions(options):
  if options.confident_regions_filename:
    return ranges.RangeSet.from_bed(options.confident_regions_filename)
  else:
    return None


def filter_candidates(candidates, select_variant_types):
  """Yields the candidate variants whose type is one of select_variant_types.

  This function iterates through candidates and yield each candidate in order
  if it satisfies any of the type constraints implied by select_variant_types.
  For example, if select_variant_types = ['snps'] this function will yield
  candidates that are bi-allelic SNPs only. Multiple select types are treated
  as OR'd together, so ['snps', 'indels'] yields candidates that are bi-allelic
  SNPs or indels.

  Args:
    candidates: Iterable of Variant protos. The candidates we want to select
      from.
    select_variant_types: List of str. The names of the variant type selectors
      we want to use to keep/remove variants. Each string must be part of
      _VARIANT_TYPE_SELECTORS or an error will be raised.

  Raises:
    ValueError: if any str in select_variant_types isn't present in
      _VARIANT_TYPE_SELECTORS.

  Yields:
    Candidates in order.
  """
  if not all(s in _VARIANT_TYPE_SELECTORS for s in select_variant_types):
    raise ValueError('Unexpected select variant type', select_variant_types)

  for candidate in candidates:
    v = candidate.variant
    for select_type in select_variant_types:
      selector = _VARIANT_TYPE_SELECTORS[select_type]
      if selector(v):
        yield candidate
        break


class RegionProcessor(object):
  """Creates DeepVariant example protos for a single region on the genome.

  This class helps us to run the very sensitive caller, pileup image creator,
  and variant labeler operations on a single region in parallel across many
  regions using the PoolExecutor API. In order to do this we need separate three
  key operations:

  (1) Collect all of the info needed to create our resources (e.g., ref reader)
      at construction. We cannot actually initialize those resources in the
      constructor, though, since we actually want different resources in each
      worker process/thread. I.e., we need lazy resource initialization.

  (2) Actually initialize these resources *after* the worker has been forked
      in our process pool. This gives us a fresh resource to use in each
      separate process.

  (3) Process the region to find candidate variants and process those into our
      tf.Example protos.
  """

  def __init__(self, options):
    """Creates a new RegionProcess.

    Args:
      options: deepvariant.DeepVariantOptions proto used to specify our
        resources for calling (e.g., reference_filename).
    """
    self.options = options
    self.initialized = False
    self.ref_reader = None
    self.sam_readers = None
    self.in_memory_sam_reader = None
    self.realigner = None
    self.pic = None
    self.labeler = None
    self.variant_caller = None
    self.samples = []
    self.population_vcf_readers = None

  def _make_allele_counter_for_region(self, region):
    return allelecounter.AlleleCounter(self.ref_reader.c_reader, region,
                                       self.options.allele_counter_options)

  def _encode_tensor(self, image_tensor):
    return image_tensor.tostring(), image_tensor.shape, 'raw'

  def _make_sam_readers(self):
    """Creates a list of SamReaders from self.options.reads_filenames."""
    logging_with_options(
        self.options,
        'Starting from v0.9.0, --use_ref_for_cram is default to true. '
        'If you are using CRAM input, note that we will decode CRAM '
        'using the reference you passed in with --ref')
    readers = []
    for reads_filename in self.options.reads_filenames:
      readers.append(
          sam.SamReader(
              reads_filename,
              ref_path=FLAGS.ref if FLAGS.use_ref_for_cram else None,
              read_requirements=self.options.read_requirements,
              parse_aux_fields=FLAGS.parse_sam_aux_fields,
              hts_block_size=FLAGS.hts_block_size,
              downsample_fraction=self.options.downsample_fraction,
              random_seed=self.options.random_seed,
              use_original_base_quality_scores=self.options
              .use_original_quality_scores))
    return readers

  def _make_population_vcf_readers(self):
    """Create a dict of VcfReader objects.

    VCF or VCF.gz file names are provided in
    self.options.population_vcf_filenames, or population_vcf_filenames.

    If the length of population_vcf_filenames is one, all variants are listed
    in one file, and one VcfReader object is created.
    If the length is greater than one, we assume there's an one-to-one mapping
    between variants and VCF files, and will return a dict.

    Raises:
      ValueError: if there are more than one VCF files containing variants
        from one chromosome.

    Returns:
      The return type depends on the length of population_vcf_filenames.
      (a) A VcfReader object if only one VCF file is provided.
      (b) A dict maps contig names to associated VcfReader objects if more than
          one VCF files are provided,.
    """
    # If only one VCF file is provided.
    if len(self.options.population_vcf_filenames) == 1:
      return vcf.VcfReader(self.options.population_vcf_filenames[0])

    # If more than one VCF files are provided.
    population_vcf_readers = {}

    for vcf_filename in self.options.population_vcf_filenames:
      population_vcf_reader = vcf.VcfReader(vcf_filename, header=None)

      # Get contig name from the first variant in a file.
      for var in population_vcf_reader:
        reference_name = var.reference_name
        break
      # There should not be more than one VCFs including variants in
      # reference_name.
      if population_vcf_readers.get(reference_name):
        raise ValueError('Variants on %s are included in multiple VCFs' %
                         reference_name)
      population_vcf_readers[reference_name] = population_vcf_reader

    return population_vcf_readers

  def initialize(self):
    """Initialize the resources needed for this work in the current env."""
    if self.initialized:
      raise ValueError('Cannot initialize this object twice')

    self.ref_reader = fasta.IndexedFastaReader(self.options.reference_filename)

    self.sam_readers = self._make_sam_readers()
    self.in_memory_sam_reader = sam.InMemorySamReader([])

    if self.options.use_allele_frequency:
      self.population_vcf_readers = self._make_population_vcf_readers()

    if self.options.realigner_enabled or \
        self.options.pic_options.alt_aligned_pileup != 'none':
      input_bam_header = sam.SamReader(self.options.reads_filenames[0]).header
      self.realigner = realigner.Realigner(
          self.options.realigner_options,
          self.ref_reader,
          shared_header=input_bam_header)

    # Organize sam readers into samples to enable generalizing to N samples.
    sample = make_examples_utils.Sample(
        sam_reader=self.sam_readers,
        in_memory_sam_reader=self.in_memory_sam_reader)

    self.samples = [sample]
    self.pic = pileup_image.PileupImageCreator(
        ref_reader=self.ref_reader,
        options=self.options.pic_options,
        samples=self.samples)

    if in_training_mode(self.options):
      self.labeler = self._make_labeler_from_options()

    self.variant_caller = self._make_variant_caller_from_options()
    self.initialized = True

  def _make_labeler_from_options(self):
    """Creates the labeler from options."""
    truth_vcf_reader = vcf.VcfReader(
        self.options.truth_variants_filename,
        excluded_format_fields=['GL', 'GQ', 'PL'])
    confident_regions = read_confident_regions(self.options)

    if (self.options.variant_caller ==
        deepvariant_pb2.DeepVariantOptions.VCF_CANDIDATE_IMPORTER):
      logging.info('For --variant_caller=vcf_candidate_importer, we '
                   'default the labeler_algorithm to positional_labler.')
      return positional_labeler.PositionalVariantLabeler(
          truth_vcf_reader=truth_vcf_reader,
          confident_regions=confident_regions)

    if (self.options.labeler_algorithm ==
        deepvariant_pb2.DeepVariantOptions.POSITIONAL_LABELER):
      return positional_labeler.PositionalVariantLabeler(
          truth_vcf_reader=truth_vcf_reader,
          confident_regions=confident_regions)
    elif (self.options.labeler_algorithm ==
          deepvariant_pb2.DeepVariantOptions.HAPLOTYPE_LABELER):
      return haplotype_labeler.HaplotypeLabeler(
          truth_vcf_reader=truth_vcf_reader,
          ref_reader=self.ref_reader,
          confident_regions=confident_regions)
    elif (self.options.labeler_algorithm ==
          deepvariant_pb2.DeepVariantOptions.CUSTOMIZED_CLASSES_LABELER):
      if (not FLAGS.customized_classes_labeler_classes_list or
          not FLAGS.customized_classes_labeler_info_field_name):
        raise ValueError('For -labeler_algorithm=customized_classes_labeler, '
                         'you need to set '
                         '-customized_classes_labeler_classes_list and '
                         '-customized_classes_labeler_info_field_name.')
      return customized_classes_labeler.CustomizedClassesVariantLabeler(
          truth_vcf_reader=truth_vcf_reader,
          confident_regions=confident_regions,
          classes_list=FLAGS.customized_classes_labeler_classes_list,
          info_field_name=FLAGS.customized_classes_labeler_info_field_name)
    else:
      raise ValueError('Unexpected labeler_algorithm',
                       self.options.labeler_algorithm)

  def _make_variant_caller_from_options(self):
    """Creates the variant_caller from options."""
    if (self.options.variant_caller ==
        deepvariant_pb2.DeepVariantOptions.VCF_CANDIDATE_IMPORTER):
      if in_training_mode(self.options):
        candidates_vcf = self.options.truth_variants_filename
      else:
        candidates_vcf = self.options.proposed_variants_filename
      return vcf_candidate_importer.VcfCandidateImporter(
          self.options.variant_caller_options, candidates_vcf)
    elif (self.options.variant_caller ==
          deepvariant_pb2.DeepVariantOptions.VERY_SENSITIVE_CALLER):
      return very_sensitive_caller.VerySensitiveCaller(
          self.options.variant_caller_options)
    else:
      raise ValueError('Unexpected variant_caller', self.options.variant_caller)

  def process(self, region):
    """Finds candidates and creates corresponding examples in a region.

    Args:
      region: A nucleus.genomics.v1.Range proto. Specifies the region on the
        genome we should process.

    Returns:
      Four values. First is a list of the found candidates, which are
      deepvariant.DeepVariantCall objects. The second value is a list of filled
      in tf.Example protos. For example, these will include the candidate
      variant, the pileup image, and, if in training mode, the truth variants
      and labels needed for training. The third value is a list of
      nucleus.genomics.v1.Variant protos containing gVCF information for all
      reference sites, if gvcf generation is enabled, otherwise returns [].
      Fourth is a dict of runtimes in seconds keyed by stage.
    """
    region_timer = timer.TimerStart()

    runtimes = {}

    before_get_reads = time.time()
    if not self.initialized:
      self.initialize()

    reads = self.region_reads(region)
    runtimes['num reads'] = len(reads)
    self.in_memory_sam_reader.replace_reads(reads)

    runtimes['get reads'] = trim_runtime(time.time() - before_get_reads)
    before_find_candidates = time.time()
    candidates, gvcfs = self.candidates_in_region(region)

    if self.options.select_variant_types:
      candidates = list(
          filter_candidates(candidates, self.options.select_variant_types))

    runtimes['find candidates'] = trim_runtime(time.time() -
                                               before_find_candidates)
    before_make_pileup_images = time.time()

    # Get allele frequencies for candidates.
    if self.options.use_allele_frequency:
      # One population VCF for all contigs.
      if len(self.options.population_vcf_filenames) == 1:
        population_vcf_reader = self.population_vcf_readers
      # One population VCF for each contig.
      # If a key is not found, set population_vcf_reader to None.
      # For example, many population studies don't include `chrM`.
      else:
        population_vcf_reader = self.population_vcf_readers.get(
            region.reference_name, None)
      candidates = list(
          self.add_allele_frequencies_to_candidates(candidates,
                                                    population_vcf_reader))

    # pylint: disable=g-complex-comprehension
    if in_training_mode(self.options):
      examples = [
          self.add_label_to_example(example, label)
          for candidate, label in self.label_candidates(candidates, region)
          for example in self.create_pileup_examples(candidate)
      ]
    else:
      examples = [
          example for candidate in candidates
          for example in self.create_pileup_examples(candidate)
      ]
    # pylint: enable=g-complex-comprehension
    logging.vlog(2, 'Found %s candidates in %s [%d bp] [%0.2fs elapsed]',
                 len(examples), ranges.to_literal(region),
                 ranges.length(region), region_timer.Stop())

    runtimes['make pileup images'] = trim_runtime(time.time() -
                                                  before_make_pileup_images)
    runtimes['num examples'] = len(examples)
    runtimes['num candidates'] = len(candidates)
    return candidates, examples, gvcfs, runtimes

  def region_reads(self, region):
    """Update in_memory_sam_reader with read alignments overlapping the region.

    If self.options.realigner_enabled is set, uses realigned reads, otherwise
    original reads are returned.

    Args:
      region: A nucleus.genomics.v1.Range object specifying the region we want
        to realign reads.

    Returns:
      [genomics.deepvariant.core.genomics.Read], reads overlapping the region.
    """
    reads = []
    if self.sam_readers is not None:
      for sam_reader_index, sam_reader in enumerate(self.sam_readers):
        try:
          reads.extend(sam_reader.query(region))
        except ValueError as err:
          error_message = str(err)
          if error_message.startswith('Data loss:'):
            raise ValueError(
                error_message + '\nFailed to parse BAM/CRAM file. '
                'This is often caused by:\n'
                '(1) When using a CRAM file, and setting '
                '--use_ref_for_cram to false (which means you want '
                'to use the embedded ref instead of a ref file), '
                'this error could be because of inability to find '
                'the embedded ref file.\n'
                '(2) Your BAM/CRAM file could be corrupted. Please '
                'check its md5.\n'
                'If you cannot find out the reason why this error '
                'is occurring, please report to '
                'https://github.com/google/deepvariant/issues')
          elif error_message.startswith('Not found: Unknown reference_name '):
            raise ValueError('{}\nThe region {} does not exist in {}.'.format(
                error_message, ranges.to_literal(region),
                self.options.reads_filenames[sam_reader_index]))
          else:
            # By default, raise the ValueError as is for now.
            raise err

    if self.options.max_reads_per_partition > 0:
      random_for_region = np.random.RandomState(self.options.random_seed)
      reads = utils.reservoir_sample(reads,
                                     self.options.max_reads_per_partition,
                                     random_for_region)
    reads = list(reads)
    if self.options.realigner_enabled:
      max_read_length_to_realign = 500
      if max_read_length_to_realign > 0:
        long_reads = [
            read for read in reads
            if len(read.aligned_sequence) > max_read_length_to_realign
        ]

        short_reads = [
            read for read in reads
            if len(read.aligned_sequence) <= max_read_length_to_realign
        ]

        _, realigned_short_reads = self.realigner.realign_reads(
            short_reads, region)

        # Long reads will be listed before short reads when both are present.
        # Examples with only short or only long reads will be unaffected.
        return long_reads + realigned_short_reads

      _, reads = self.realigner.realign_reads(reads, region)
    return reads

  def candidates_in_region(self, region):
    """Finds candidate DeepVariantCall protos in region.

    Args:
      region: A nucleus.genomics.v1.Range object specifying the region we want
        to get candidates for.

    Returns:
      A 2-tuple. The first value is a list of deepvariant_pb2.DeepVariantCalls
      objects, in coordidate order. The second value is a list of
      nucleus.genomics.v1.Variant protos containing gVCF information for all
      reference sites, if gvcf generation is enabled, otherwise returns [].
    """
    reads = self.in_memory_sam_reader.query(region)
    if not reads and not gvcf_output_enabled(self.options):
      # If we are generating gVCF output we cannot safely abort early here as
      # we need to return the gVCF records calculated by the caller below.
      return [], []

    allele_counter = self._make_allele_counter_for_region(region)
    for read in reads:
      allele_counter.add(read, self.options.variant_caller_options.sample_name)

    candidates, gvcfs = self.variant_caller.calls_and_gvcfs(
        allele_counter, gvcf_output_enabled(self.options))
    return candidates, gvcfs

  def align_to_all_haplotypes(self, variant, reads):
    """For each alternate allele, realign reads to it and get "ref" sequences.

    For alt-aligned pileups, this realigns the reads to each of the alternate
    haplotypes. It also outputs the sequence for each alternate allele, which
    is also needed to build the pileup image.

    Args:
      variant: a nucleus.genomics.v1.Variant containing the alt alleles to align
        against.
      reads: a list of reads (nucleus.genomics.v1.Read) to be realigned around
        the variant.

    Returns:
      dict of alignments keyed by haplotype, dict of window sequences keyed by
          haplotype.
    """

    window_width = self.pic.width
    window_half_width = self.pic.half_width

    alt_alleles = list(variant.alternate_bases)
    contig = variant.reference_name
    ref_start = variant.start
    ref_bases = variant.reference_bases
    ref_end = ref_start + len(ref_bases)

    # Sanity check that the reference_bases in the variant match the reference.
    ref_query_at_variant = self.realigner.ref_reader.query(
        ranges.make_range(contig, ref_start, ref_end))
    if ref_bases != ref_query_at_variant:
      raise ValueError('Error: reference_bases property in variant ({})'
                       'does not match the bases in the reference ({}) at that '
                       'position.'.format(ref_bases, ref_query_at_variant))

    # Margin must be equal to or more than half the window width.
    # Some extra prefix/suffix can be added to anchor alignments, but currently
    # we don't add extra.
    margin = window_half_width
    valid_end = min(
        self.realigner.ref_reader.contig(contig).n_bases, ref_end + margin)
    alignment_region = ranges.make_range(contig, max(ref_start - margin, 0),
                                         valid_end)
    trimmed_reads = [realigner.trim_read(r, alignment_region) for r in reads]
    # Filter reads to a minimum read length of 15 bp after trimming.
    reads = [r for r in trimmed_reads if len(r.aligned_sequence) >= 15]
    prefix = self.realigner.ref_reader.query(
        ranges.make_range(contig, max(ref_start - margin, 0), ref_start))
    suffix = self.realigner.ref_reader.query(
        ranges.make_range(contig, ref_end, valid_end))

    alignments_by_haplotype = {}
    sequences_by_haplotype = {}
    for hap in alt_alleles:
      # Align to each of the alt_alleles:
      alignments_by_haplotype[hap] = self.realigner.align_to_haplotype(
          this_haplotype=hap,
          haplotypes=[hap],
          prefix=prefix,
          suffix=suffix,
          reads=reads,
          contig=contig,
          ref_start=ref_start - len(prefix))
      # Sequence of the alt haplotype in the window:
      end_of_prefix = prefix[-window_half_width:]
      beginning_of_suffix = suffix[:max(window_half_width + 1 - len(hap), 0)]
      sequences_by_haplotype[hap] = end_of_prefix + hap + beginning_of_suffix
      # Long haplotypes can extend past the window, so enforce the width here.
      sequences_by_haplotype[hap] = sequences_by_haplotype[hap][0:window_width]
    return {
        'alt_alignments': alignments_by_haplotype,
        'alt_sequences': sequences_by_haplotype
    }

  def add_allele_frequencies_to_candidates(self, candidates,
                                           population_vcf_reader):
    """Adds allele frequencies for candidate variants.

    Args:
      candidates: Iterable of DeepVariantCall protos that are the candidates we
        want to process.
      population_vcf_reader: A VcfReader object that reads the associated
        population VCF file for candidates. None if the contig is not found.

    Yields:
      DeepVariantCall protos. The same set of input candidates, with field
        allele_frequency filled.
    """
    for candidate in candidates:
      if population_vcf_reader:
        dict_allele_frequency = find_matching_allele_frequency(
            variant=candidate.variant,
            population_vcf_reader=population_vcf_reader,
            ref_reader=self.ref_reader)
      else:
        # Set ALT frequencies to 0 if population_vcf_reader is None.
        dict_allele_frequency = {}
        dict_allele_frequency[candidate.variant.reference_bases] = 1
        for alt in candidate.variant.alternate_bases:
          dict_allele_frequency[alt] = 0

      yield deepvariant_pb2.DeepVariantCall(
          variant=candidate.variant,
          allele_support=candidate.allele_support,
          allele_frequency=dict_allele_frequency)

  def create_pileup_examples(self, dv_call):
    """Creates a tf.Example for DeepVariantCall.

    This function calls PileupImageCreator.create_pileup_images on dv_call to
    get raw image tensors for each alt_allele option (see docs for details).
    These tensors are encoded as pngs, and all of the key information is encoded
    as a tf.Example via a call to tf_utils.make_example.

    Args:
      dv_call: A DeepVariantCall proto.

    Returns:
      A list of tf.Example protos.
    """
    reads_for_samples = [
        self.pic.get_reads(
            dv_call.variant, sam_reader=sample.in_memory_sam_reader)
        for sample in self.samples
    ]
    logging.vlog(
        3, 'create_pileup_examples for variant: {}:{}_{}'.format(
            dv_call.variant.reference_name, dv_call.variant.start,
            dv_call.variant.reference_bases))

    # Decide whether each candidate needs ALT-alignment.
    alt_align_this_variant = False
    if self.options.pic_options.alt_aligned_pileup != 'none':
      if self.options.pic_options.types_to_alt_align == 'indels':
        alt_align_this_variant = variant_utils.is_indel(dv_call.variant)
      else:  # types_to_alt_align can only be 'all' or 'indels'.
        alt_align_this_variant = True

    haplotype_alignments_for_samples = None
    haplotype_sequences = None
    if alt_align_this_variant:
      # Align the reads against each alternate allele, saving the sequences of
      # those alleles along with the alignments for pileup images.
      alt_info_for_samples = [
          self.align_to_all_haplotypes(dv_call.variant, reads)
          for reads in reads_for_samples
      ]
      # Each sample has different reads and thus different alt-alignments.
      haplotype_alignments_for_samples = [
          sample['alt_alignments'] for sample in alt_info_for_samples
      ]
      # All samples share the same alt sequences, so select the first one.
      haplotype_sequences = alt_info_for_samples[0]['alt_sequences']

    pileup_images = self.pic.create_pileup_images(
        dv_call=dv_call,
        reads_for_samples=reads_for_samples,
        haplotype_alignments_for_samples=haplotype_alignments_for_samples,
        haplotype_sequences=haplotype_sequences)

    if pileup_images is None:
      # We cannot build a PileupImage for dv_call, issue a warning.
      logging.warning('Could not create PileupImage for candidate at %s:%s',
                      dv_call.variant.reference_name, dv_call.variant.start)
      return []

    examples = []
    for alt_alleles, image_tensor in pileup_images:
      encoded_tensor, shape, tensor_format = self._encode_tensor(image_tensor)
      examples.append(
          tf_utils.make_example(
              dv_call.variant,
              alt_alleles,
              encoded_tensor,
              shape=shape,
              image_format=tensor_format,
              sequencing_type=self.options.pic_options.sequencing_type))
    return examples

  def label_candidates(self, candidates, region):
    """Gets label information for each candidate.

    Args:
      candidates: list[DeepVariantCalls]: The list of candidate variant calls we
        want to label.
      region: A nucleus.genomics.v1.Range object specifying the region we want
        to get candidates for.

    Yields:
      Tuples of (candidate, label_variants.Label objects) for each candidate in
      candidates that could be assigned a label. Candidates that couldn't be
      labeled will not be returned.
    """
    # Get our list of labels for each candidate variant.
    labels = self.labeler.label_variants(
        [candidate.variant for candidate in candidates], region)

    # Remove any candidates we couldn't label, yielding candidate, label pairs.
    for candidate, label in zip(candidates, labels):
      if label.is_confident:
        yield candidate, label

  def add_label_to_example(self, example, label):
    """Adds label information about the assigned label to our example.

    Args:
      example: A tf.Example proto. We will write truth_variant and label into
        this proto.
      label: A variant_labeler.Label object containing the labeling information
        to add to our example.

    Returns:
      The example proto with label fields added.

    Raises:
      ValueError: if label isn't confident.
    """
    if not label.is_confident:
      raise ValueError('Cannot add a non-confident label to an example',
                       example, label)
    alt_alleles_indices = tf_utils.example_alt_alleles_indices(example)

    tf_utils.example_set_variant(example, label.variant)

    # Set the label of the example to the # alts given our alt_alleles_indices.
    tf_utils.example_set_label(example,
                               label.label_for_alt_alleles(alt_alleles_indices))
    return example


def processing_regions_from_options(options):
  """Computes the calling regions from our options.

  This function does all of the work needed to read our input files and region
  specifications to determine the list of regions we should generate examples
  over. It also computes the confident regions needed to label variants.

  Args:
    options: deepvariant.DeepVariantOptions proto containing information about
      our input data sources.

  Raises:
    ValueError: if the regions to call is empty.

  Returns:
    Two values. The first is a list of nucleus.genomics.v1.Range protos of the
    regions we should process. The second is a RangeSet containing the confident
    regions for labeling, or None if we are running in training mode.
  """
  ref_contigs = fasta.IndexedFastaReader(
      options.reference_filename).header.contigs

  # Add in confident regions and vcf_contigs if in training mode.
  vcf_contigs = None
  if in_training_mode(options):
    vcf_contigs = vcf.VcfReader(options.truth_variants_filename).header.contigs

  all_sam_contigs = [
      sam.SamReader(reads_file).header.contigs
      for reads_file in options.reads_filenames
  ]
  sam_contigs = common_contigs(only_true(*all_sam_contigs))

  contigs = _ensure_consistent_contigs(ref_contigs, sam_contigs, vcf_contigs,
                                       options.exclude_contigs,
                                       options.min_shared_contigs_basepairs)
  logging_with_options(options,
                       'Common contigs are %s' % [c.name for c in contigs])
  calling_regions = build_calling_regions(ref_contigs, options.calling_regions,
                                          options.exclude_calling_regions)
  if not calling_regions:
    raise ValueError('The regions to call is empty. Check your --regions and '
                     '--exclude_regions flags to make sure they are not '
                     'resulting in set of empty region to process. This also '
                     'happens if you use "chr20" for a BAM where contig names '
                     'don\'t have "chr"s (or vice versa).')
  regions = regions_to_process(
      contigs=contigs,
      partition_size=options.allele_counter_options.partition_size,
      calling_regions=calling_regions,
      task_id=options.task_id,
      num_shards=options.num_shards)

  region_list = list(regions)
  # When processing many regions, check for a VCF to narrow down the regions.
  if not gvcf_output_enabled(options) and len(region_list) > 10000:
    if in_training_mode(options):
      filter_vcf = options.truth_variants_filename
      logging_with_options(
          options, 'Reading VCF to see if we can skip processing some regions '
          'without variants in the --truth_variants VCF.')
    else:
      filter_vcf = options.proposed_variants_filename
      if filter_vcf:
        logging_with_options(
            options, 'Reading VCF to skip processing some regions without '
            'variants in the --proposed_variants VCF.')
    if filter_vcf:
      before = time.time()
      variant_positions = []
      with vcf.VcfReader(filter_vcf) as vcf_reader:
        for variant in vcf_reader:
          variant_positions.append(variant_utils.variant_position(variant))

      filtered_regions = filter_regions_by_vcf(region_list, variant_positions)
      time_elapsed = time.time() - before
      logging_with_options(
          options,
          'Filtering regions took {} seconds and reduced the number of '
          'regions to process from {} to {} regions containing variants '
          'from the supplied VCF.'.format(
              round(time_elapsed, 2), len(region_list), len(filtered_regions)))
      return filtered_regions
  return region_list


# redacted
class OutputsWriter(object):
  """Manages all of the outputs of make_examples in a single place."""

  def __init__(self, options):
    self._writers = {
        k: None for k in ['candidates', 'examples', 'gvcfs', 'runtime']
    }

    if options.candidates_filename:
      self._add_writer('candidates',
                       tfrecord.Writer(options.candidates_filename))

    if options.examples_filename:
      self._add_writer('examples', tfrecord.Writer(options.examples_filename))

    if options.gvcf_filename:
      self._add_writer('gvcfs', tfrecord.Writer(options.gvcf_filename))

    if options.runtime_by_region:
      self._add_writer('runtime',
                       tf.io.gfile.GFile(options.runtime_by_region, mode='w'))
      writer = self._writers['runtime']
      writer.__enter__()
      writer.write('\t'.join(RUNTIME_BY_REGION_COLUMNS) + '\n')

  def write_examples(self, *examples):
    self._write('examples', *examples)

  def write_gvcfs(self, *gvcfs):
    self._write('gvcfs', *gvcfs)

  def write_candidates(self, *candidates):
    self._write('candidates', *candidates)

  def write_runtime(self, stats_dict):
    columns = [str(stats_dict.get(k, 'NA')) for k in RUNTIME_BY_REGION_COLUMNS]
    writer = self._writers['runtime']
    writer.write('\t'.join(columns) + '\n')

  def _add_writer(self, name, writer):
    if name not in self._writers:
      raise ValueError(
          'Expected writer {} to have a None binding in writers.'.format(name))
    if self._writers[name] is not None:
      raise ValueError('Expected writer {} to be bound to None in writers but '
                       'saw {} instead'.format(name, self._writers[name]))
    self._writers[name] = writer

  def __enter__(self):
    """API function to support with syntax."""
    for writer in self._writers.values():
      if writer is not None:
        writer.__enter__()
    return self

  def __exit__(self, exception_type, exception_value, traceback):
    for writer in self._writers.values():
      if writer is not None:
        writer.__exit__(exception_type, exception_value, traceback)

  def _write(self, writer_name, *protos):
    writer = self._writers[writer_name]
    if writer:
      for proto in protos:
        writer.write(proto)


def make_examples_runner(options):
  """Runs examples creation stage of deepvariant."""
  resource_monitor = resources.ResourceMonitor().start()
  before_initializing_inputs = time.time()

  logging_with_options(options, 'Preparing inputs')
  regions = processing_regions_from_options(options)

  # Create a processor to create candidates and examples for each region.
  region_processor = RegionProcessor(options)
  region_processor.initialize()

  logging_with_options(options,
                       'Writing examples to %s' % options.examples_filename)
  if options.candidates_filename:
    logging_with_options(
        options, 'Writing candidates to %s' % options.candidates_filename)
  if options.gvcf_filename:
    logging_with_options(options,
                         'Writing gvcf records to %s' % options.gvcf_filename)

  n_regions, n_candidates, n_examples = 0, 0, 0
  last_reported = 0
  with OutputsWriter(options) as writer:
    logging_with_options(
        options, 'Overhead for preparing inputs: %d seconds' %
        (time.time() - before_initializing_inputs))

    running_timer = timer.TimerStart()
    for region in regions:
      candidates, examples, gvcfs, runtimes = region_processor.process(region)
      n_candidates += len(candidates)
      n_examples += len(examples)
      n_regions += 1

      before_write_outputs = time.time()
      writer.write_candidates(*candidates)

      # If we have any gvcf records, write them out. This also serves to
      # protect us from trying to write to the gvcfs output of writer when gvcf
      # generation is turned off. In that case, gvcfs will always be empty and
      # we'll never execute the write.
      if gvcfs:
        writer.write_gvcfs(*gvcfs)
      writer.write_examples(*examples)
      if options.runtime_by_region:
        runtimes['write outputs'] = trim_runtime(time.time() -
                                                 before_write_outputs)
        runtimes['region'] = ranges.to_literal(region)
        writer.write_runtime(stats_dict=runtimes)

      # Output timing for every N candidates.
      # redacted
      if (int(n_candidates / FLAGS.logging_every_n_candidates) > last_reported
          or n_regions == 1):
        last_reported = int(n_candidates / FLAGS.logging_every_n_candidates)
        logging_with_options(
            options, '%s candidates (%s examples) [%0.2fs elapsed]' %
            (n_candidates, n_examples, running_timer.Stop()))
        running_timer = timer.TimerStart()

  # Construct and then write out our MakeExamplesRunInfo proto.
  if options.run_info_filename:
    run_info = deepvariant_pb2.MakeExamplesRunInfo(
        options=options, resource_metrics=resource_monitor.metrics())
    if in_training_mode(options):
      if region_processor.labeler.metrics is not None:
        run_info.labeling_metrics.CopyFrom(region_processor.labeler.metrics)
      else:
        logging.warning(
            'Labeling metrics requested but the selected labeling '
            'algorithm %s does not collect metrics; skipping.',
            options.labeler_algorithm)
    logging_with_options(
        options,
        'Writing MakeExamplesRunInfo to %s' % options.run_info_filename)
    write_make_examples_run_info(run_info, path=options.run_info_filename)

  logging_with_options(options, 'Found %s candidate variants' % n_candidates)
  logging_with_options(options, 'Created %s examples' % n_examples)


def main(argv=()):
  with errors.clean_commandline_error_exit():
    if len(argv) > 1:
      errors.log_and_raise(
          'Command line parsing failure: make_examples does not accept '
          'positional arguments but some are present on the command line: '
          '"{}".'.format(str(argv)), errors.CommandLineError)
    del argv  # Unused.

    proto_utils.uses_fast_cpp_protos_or_die()

    logging_level.set_from_flag()
    hts_verbose.set(hts_verbose.htsLogLevel[FLAGS.hts_logging_level])

    # Set up options; may do I/O.
    options = default_options(add_flags=True, flags_obj=FLAGS)

    # Check arguments that apply to any mode.
    if not options.reference_filename:
      errors.log_and_raise('ref argument is required.', errors.CommandLineError)
    if not options.reads_filenames:
      errors.log_and_raise('reads argument is required.',
                           errors.CommandLineError)
    if not options.examples_filename:
      errors.log_and_raise('examples argument is required.',
                           errors.CommandLineError)
    if options.n_cores != 1:
      errors.log_and_raise(
          'Currently only supports n_cores == 1 but got {}.'.format(
              options.n_cores), errors.CommandLineError)

    # Check for argument issues specific to different modes.
    if in_training_mode(options):
      if not options.truth_variants_filename:
        errors.log_and_raise(
            'truth_variants is required when in training mode.',
            errors.CommandLineError)
      if not options.confident_regions_filename:
        if options.variant_caller == \
            deepvariant_pb2.DeepVariantOptions.VCF_CANDIDATE_IMPORTER:
          logging.info('Note: --confident_regions is optional with '
                       'vcf_candidate_importer. '
                       'You did not specify --confident_regions, which means '
                       'examples will be generated for the whole region.')
        else:
          errors.log_and_raise(
              'confident_regions is required when in training mode.',
              errors.CommandLineError)
      if options.gvcf_filename:
        errors.log_and_raise('gvcf is not allowed in training mode.',
                             errors.CommandLineError)
      if (options.variant_caller == \
          deepvariant_pb2.DeepVariantOptions.VCF_CANDIDATE_IMPORTER and
          options.proposed_variants_filename):
        errors.log_and_raise(
            '--proposed_variants should not be used with '
            'vcf_candidate_importer in training mode. '
            'Use --truth_variants to pass in the candidates '
            'with correct labels for training.', errors.CommandLineError)
    else:
      # Check for argument issues specific to calling mode.
      if options.truth_variants_filename:
        errors.log_and_raise('Do not specify --truth_variants in calling mode.',
                             errors.CommandLineError)
      if options.variant_caller_options.sample_name == _UNKNOWN_SAMPLE:
        errors.log_and_raise('sample_name must be specified in calling mode.',
                             errors.CommandLineError)
      if options.variant_caller_options.gq_resolution < 1:
        errors.log_and_raise('gq_resolution must be a non-negative integer.',
                             errors.CommandLineError)
      if options.variant_caller == \
          deepvariant_pb2.DeepVariantOptions.VCF_CANDIDATE_IMPORTER:
        if not options.proposed_variants_filename:
          errors.log_and_raise(
              '--proposed_variants is required with vcf_candidate_importer in '
              'calling mode.', errors.CommandLineError)

    # Run!
    make_examples_runner(options)


if __name__ == '__main__':
  flags.mark_flags_as_required([
      'examples',
      'mode',
      'reads',
      'ref',
  ])
  app.run(main)
