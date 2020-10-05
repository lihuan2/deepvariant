# Copyright 2019 Google LLC.
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
"""Tests for deepvariant .run_deeptrio."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import sys
if 'google' in sys.modules and 'google.protobuf' not in sys.modules:
  del sys.modules['google']



from absl import flags
from absl.testing import absltest
from absl.testing import parameterized
import six
from deepvariant.opensource_only.scripts import run_deeptrio
from deepvariant.testing import flagsaver

FLAGS = flags.FLAGS


# pylint: disable=line-too-long
class RunDeeptrioTest(parameterized.TestCase):

  @parameterized.parameters('WGS', 'WES', 'PACBIO')
  @flagsaver.FlagSaver
  def test_basic_command(self, model_type):
    FLAGS.model_type = model_type
    FLAGS.ref = 'your_ref'
    FLAGS.reads_child = 'your_bam_child'
    FLAGS.reads_parent1 = 'your_bam_parent1'
    FLAGS.reads_parent2 = 'your_bam_parent2'
    FLAGS.sample_name_child = 'your_sample_child'
    FLAGS.sample_name_parent1 = 'your_sample_parent1'
    FLAGS.sample_name_parent2 = 'your_sample_parent2'
    FLAGS.output_vcf_child = 'your_vcf_child'
    FLAGS.output_vcf_parent1 = 'your_vcf_parent1'
    FLAGS.output_vcf_parent2 = 'your_vcf_parent2'
    FLAGS.output_gvcf_child = 'your_gvcf_child'
    FLAGS.output_gvcf_parent1 = 'your_gvcf_parent1'
    FLAGS.output_gvcf_parent2 = 'your_gvcf_parent2'
    FLAGS.output_gvcf_merged = 'your_gvcf_merged'
    FLAGS.num_shards = 64
    commands = run_deeptrio.create_all_commands('/tmp/deeptrio_tmp_output')

    extra_args_plus_gvcf = (
        '--gvcf "/tmp/deeptrio_tmp_output/gvcf.tfrecord@64.gz" '
        '--pileup_image_height_child "60" '
        '--pileup_image_height_parent "40" ')
    if model_type == 'PACBIO':
      # --gvcf is added in the middle because the flags are sorted
      # alphabetically. PacBio flags here is now mixed with some others.
      extra_args_plus_gvcf = (
          '--alt_aligned_pileup "diff_channels" '
          '--gvcf "/tmp/deeptrio_tmp_output/gvcf.tfrecord@64.gz" '
          '--pileup_image_height_child "60" '
          '--pileup_image_height_parent "40" '
          '--norealign_reads '
          '--vsc_min_fraction_indels "0.12" ')
    if model_type == 'WES':
      extra_args_plus_gvcf = (
          '--gvcf "/tmp/deeptrio_tmp_output/gvcf.tfrecord@64.gz" '
          '--pileup_image_height_child "100" '
          '--pileup_image_height_parent "100" ')

    self.assertEqual(
        commands[0], 'time seq 0 63 '
        '| parallel -q --halt 2 --line-buffer '
        '/opt/deepvariant/bin/deeptrio/make_examples '
        '--mode calling '
        '--ref "your_ref" '
        '--reads_parent1 "your_bam_parent1" '
        '--reads_parent2 "your_bam_parent2" '
        '--reads "your_bam_child" '
        '--examples "/tmp/deeptrio_tmp_output/make_examples.tfrecord@64.gz" '
        '--sample_name "your_sample_child" '
        '--sample_name_parent1 "your_sample_parent1" '
        '--sample_name_parent2 "your_sample_parent2" '
        '%s'
        '--task {}' % extra_args_plus_gvcf)
    self.assertEqual(
        commands[1], 'time /opt/deepvariant/bin/call_variants '
        '--outfile '
        '"/tmp/deeptrio_tmp_output/call_variants_output_child.tfrecord.gz" '
        '--examples "/tmp/deeptrio_tmp_output/make_examples_child.tfrecord@64.gz" '
        '--checkpoint "/opt/models/deeptrio/{}/child/model.ckpt"'.format(
            model_type.lower()))
    self.assertEqual(
        commands[2], 'time /opt/deepvariant/bin/call_variants '
        '--outfile '
        '"/tmp/deeptrio_tmp_output/call_variants_output_parent1.tfrecord.gz" '
        '--examples "/tmp/deeptrio_tmp_output/make_examples_parent1.tfrecord@64.gz" '
        '--checkpoint "/opt/models/deeptrio/{}/parent/model.ckpt"'.format(
            model_type.lower()))
    self.assertEqual(
        commands[3], 'time /opt/deepvariant/bin/call_variants '
        '--outfile '
        '"/tmp/deeptrio_tmp_output/call_variants_output_parent2.tfrecord.gz" '
        '--examples "/tmp/deeptrio_tmp_output/make_examples_parent2.tfrecord@64.gz" '
        '--checkpoint "/opt/models/deeptrio/{}/parent/model.ckpt"'.format(
            model_type.lower()))
    self.assertEqual(
        commands[4], 'time /opt/deepvariant/bin/postprocess_variants '
        '--ref "your_ref" '
        '--infile '
        '"/tmp/deeptrio_tmp_output/call_variants_output_child.tfrecord.gz" '
        '--outfile "your_vcf_child" '
        '--nonvariant_site_tfrecord_path '
        '"/tmp/deeptrio_tmp_output/gvcf_child.tfrecord@64.gz" '
        '--gvcf_outfile "your_gvcf_child"')
    self.assertEqual(
        commands[5], 'time /opt/deepvariant/bin/postprocess_variants '
        '--ref "your_ref" '
        '--infile '
        '"/tmp/deeptrio_tmp_output/call_variants_output_parent1.tfrecord.gz" '
        '--outfile "your_vcf_parent1" '
        '--nonvariant_site_tfrecord_path '
        '"/tmp/deeptrio_tmp_output/gvcf_parent1.tfrecord@64.gz" '
        '--gvcf_outfile "your_gvcf_parent1"')
    self.assertEqual(
        commands[6], 'time /opt/deepvariant/bin/postprocess_variants '
        '--ref "your_ref" '
        '--infile '
        '"/tmp/deeptrio_tmp_output/call_variants_output_parent2.tfrecord.gz" '
        '--outfile "your_vcf_parent2" '
        '--nonvariant_site_tfrecord_path '
        '"/tmp/deeptrio_tmp_output/gvcf_parent2.tfrecord@64.gz" '
        '--gvcf_outfile "your_gvcf_parent2"')
    self.assertLen(commands, 7)

  @parameterized.parameters('WGS', 'WES', 'PACBIO')
  @flagsaver.FlagSaver
  def test_duo_command(self, model_type):
    FLAGS.model_type = model_type
    FLAGS.ref = 'your_ref'
    FLAGS.reads_child = 'your_bam_child'
    FLAGS.reads_parent1 = 'your_bam_parent1'
    FLAGS.sample_name_child = 'your_sample_child'
    FLAGS.sample_name_parent1 = 'your_sample_parent1'
    FLAGS.output_vcf_child = 'your_vcf_child'
    FLAGS.output_vcf_parent1 = 'your_vcf_parent1'
    FLAGS.output_gvcf_child = 'your_gvcf_child'
    FLAGS.output_gvcf_parent1 = 'your_gvcf_parent1'
    FLAGS.output_gvcf_merged = 'your_gvcf_merged'
    FLAGS.num_shards = 64
    commands = run_deeptrio.create_all_commands('/tmp/deeptrio_tmp_output')

    extra_args_plus_gvcf = (
        '--gvcf "/tmp/deeptrio_tmp_output/gvcf.tfrecord@64.gz" '
        '--pileup_image_height_child "60" '
        '--pileup_image_height_parent "40" ')
    if model_type == 'PACBIO':
      # --gvcf is added in the middle because the flags are sorted
      # alphabetically. PacBio flags here is now mixed with some others.
      extra_args_plus_gvcf = (
          '--alt_aligned_pileup "diff_channels" '
          '--gvcf "/tmp/deeptrio_tmp_output/gvcf.tfrecord@64.gz" '
          '--pileup_image_height_child "60" '
          '--pileup_image_height_parent "40" '
          '--norealign_reads '
          '--vsc_min_fraction_indels "0.12" ')
    if model_type == 'WES':
      extra_args_plus_gvcf = (
          '--gvcf "/tmp/deeptrio_tmp_output/gvcf.tfrecord@64.gz" '
          '--pileup_image_height_child "100" '
          '--pileup_image_height_parent "100" ')

    self.assertEqual(
        commands[0], 'time seq 0 63 '
        '| parallel -q --halt 2 --line-buffer '
        '/opt/deepvariant/bin/deeptrio/make_examples '
        '--mode calling '
        '--ref "your_ref" '
        '--reads_parent1 "your_bam_parent1" '
        '--reads "your_bam_child" '
        '--examples "/tmp/deeptrio_tmp_output/make_examples.tfrecord@64.gz" '
        '--sample_name "your_sample_child" '
        '--sample_name_parent1 "your_sample_parent1" '
        '%s'
        '--task {}' % extra_args_plus_gvcf)
    self.assertEqual(
        commands[1], 'time /opt/deepvariant/bin/call_variants '
        '--outfile '
        '"/tmp/deeptrio_tmp_output/call_variants_output_child.tfrecord.gz" '
        '--examples "/tmp/deeptrio_tmp_output/make_examples_child.tfrecord@64.gz" '
        '--checkpoint "/opt/models/deeptrio/{}/child/model.ckpt"'.format(
            model_type.lower()))
    self.assertEqual(
        commands[2], 'time /opt/deepvariant/bin/call_variants '
        '--outfile '
        '"/tmp/deeptrio_tmp_output/call_variants_output_parent1.tfrecord.gz" '
        '--examples "/tmp/deeptrio_tmp_output/make_examples_parent1.tfrecord@64.gz" '
        '--checkpoint "/opt/models/deeptrio/{}/parent/model.ckpt"'.format(
            model_type.lower()))
    self.assertEqual(
        commands[3], 'time /opt/deepvariant/bin/postprocess_variants '
        '--ref "your_ref" '
        '--infile '
        '"/tmp/deeptrio_tmp_output/call_variants_output_child.tfrecord.gz" '
        '--outfile "your_vcf_child" '
        '--nonvariant_site_tfrecord_path '
        '"/tmp/deeptrio_tmp_output/gvcf_child.tfrecord@64.gz" '
        '--gvcf_outfile "your_gvcf_child"')
    self.assertEqual(
        commands[4], 'time /opt/deepvariant/bin/postprocess_variants '
        '--ref "your_ref" '
        '--infile '
        '"/tmp/deeptrio_tmp_output/call_variants_output_parent1.tfrecord.gz" '
        '--outfile "your_vcf_parent1" '
        '--nonvariant_site_tfrecord_path '
        '"/tmp/deeptrio_tmp_output/gvcf_parent1.tfrecord@64.gz" '
        '--gvcf_outfile "your_gvcf_parent1"')
    # pylint: disable=g-generic-assert
    self.assertLen(commands, 5)

  @parameterized.parameters(
      (None, '--alt_aligned_pileup "diff_channels" '
       '--gvcf "/tmp/deeptrio_tmp_output/gvcf.tfrecord@64.gz" '
       '--pileup_image_height_child "60" '
       '--pileup_image_height_parent "40" '
       '--norealign_reads '
       '--vsc_min_fraction_indels "0.12" '),
      ('alt_aligned_pileup="rows",vsc_min_fraction_indels=0.03',
       '--alt_aligned_pileup "rows" '
       '--gvcf "/tmp/deeptrio_tmp_output/gvcf.tfrecord@64.gz" '
       '--pileup_image_height_child "60" '
       '--pileup_image_height_parent "40" '
       '--norealign_reads '
       '--vsc_min_fraction_indels "0.03" '),
  )
  @flagsaver.FlagSaver
  def test_pacbio_args_overwrite(self, make_examples_extra_args, expected_args):
    """Confirms that adding extra flags can overwrite the default from mode."""
    FLAGS.model_type = 'PACBIO'
    FLAGS.ref = 'your_ref'
    FLAGS.sample_name_child = 'your_sample_child'
    FLAGS.sample_name_parent1 = 'your_sample_parent1'
    FLAGS.sample_name_parent2 = 'your_sample_parent2'
    FLAGS.reads_child = 'your_bam_child'
    FLAGS.reads_parent1 = 'your_bam_parent1'
    FLAGS.reads_parent2 = 'your_bam_parent2'
    FLAGS.output_vcf_child = 'your_vcf_child'
    FLAGS.output_vcf_parent1 = 'your_vcf_parent1'
    FLAGS.output_vcf_parent2 = 'your_vcf_parent2'
    FLAGS.output_gvcf_child = 'your_gvcf_child'
    FLAGS.output_gvcf_parent1 = 'your_gvcf_parent1'
    FLAGS.output_gvcf_parent2 = 'your_gvcf_parent2'
    FLAGS.num_shards = 64
    FLAGS.regions = None
    FLAGS.make_examples_extra_args = make_examples_extra_args
    commands = run_deeptrio.create_all_commands('/tmp/deeptrio_tmp_output')
    self.assertEqual(
        commands[0], 'time seq 0 63 | parallel -q --halt 2 --line-buffer '
        '/opt/deepvariant/bin/deeptrio/make_examples --mode calling '
        '--ref "your_ref" --reads_parent1 "your_bam_parent1" '
        '--reads_parent2 "your_bam_parent2" '
        '--reads "your_bam_child" '
        '--examples "/tmp/deeptrio_tmp_output/make_examples.tfrecord@64.gz" '
        '--sample_name "your_sample_child" '
        '--sample_name_parent1 "your_sample_parent1" '
        '--sample_name_parent2 "your_sample_parent2" '
        '%s'
        '--task {}' % expected_args)

  @parameterized.parameters(
      ('chr1:20-30', '--pileup_image_height_child "60" '
       '--pileup_image_height_parent "40" '
       '--regions "chr1:20-30"'),
      ('chr1:20-30 chr2:100-200', '--pileup_image_height_child "60" '
       '--pileup_image_height_parent "40" '
       '--regions "chr1:20-30 chr2:100-200"'),
      ("'chr1:20-30 chr2:100-200'", '--pileup_image_height_child "60" '
       '--pileup_image_height_parent "40" '
       "--regions 'chr1:20-30 chr2:100-200'"),
  )
  def test_make_examples_regions(self, regions, expected_args):
    FLAGS.model_type = 'WGS'
    FLAGS.ref = 'your_ref'
    FLAGS.sample_name_child = 'your_sample_child'
    FLAGS.sample_name_parent1 = 'your_sample_parent1'
    FLAGS.sample_name_parent2 = 'your_sample_parent2'
    FLAGS.reads_child = 'your_bam_child'
    FLAGS.reads_parent1 = 'your_bam_parent1'
    FLAGS.reads_parent2 = 'your_bam_parent2'
    FLAGS.output_vcf_child = 'your_vcf_child'
    FLAGS.output_vcf_parent1 = 'your_vcf_parent1'
    FLAGS.output_vcf_parent2 = 'your_vcf_parent2'
    FLAGS.num_shards = 64
    FLAGS.regions = regions
    commands = run_deeptrio.create_all_commands('/tmp/deeptrio_tmp_output')

    self.assertEqual(
        commands[0], 'time seq 0 63 | parallel -q --halt 2 --line-buffer '
        '/opt/deepvariant/bin/deeptrio/make_examples --mode calling '
        '--ref "your_ref" --reads_parent1 "your_bam_parent1" '
        '--reads_parent2 "your_bam_parent2" '
        '--reads "your_bam_child" '
        '--examples "/tmp/deeptrio_tmp_output/make_examples.tfrecord@64.gz" '
        '--sample_name "your_sample_child" '
        '--sample_name_parent1 "your_sample_parent1" '
        '--sample_name_parent2 "your_sample_parent2" '
        '%s '
        '--task {}' % expected_args)

  @flagsaver.FlagSaver
  def test_make_examples_extra_args_invalid(self):
    FLAGS.model_type = 'WGS'
    FLAGS.ref = 'your_ref'
    FLAGS.sample_name_child = 'your_sample_child'
    FLAGS.sample_name_parent1 = 'your_sample_parent1'
    FLAGS.sample_name_parent2 = 'your_sample_parent2'
    FLAGS.reads_child = 'your_bam_child'
    FLAGS.reads_parent1 = 'your_bam_parent1'
    FLAGS.reads_parent2 = 'your_bam_parent2'
    FLAGS.output_vcf_child = 'your_vcf_child'
    FLAGS.output_vcf_parent1 = 'your_vcf_parent1'
    FLAGS.output_vcf_parent2 = 'your_vcf_parent2'
    FLAGS.output_gvcf_child = 'your_gvcf_child'
    FLAGS.output_gvcf_parent1 = 'your_gvcf_parent1'
    FLAGS.output_gvcf_parent2 = 'your_gvcf_parent2'
    FLAGS.num_shards = 64
    FLAGS.make_examples_extra_args = 'keep_secondary_alignments'
    with six.assertRaisesRegex(self, ValueError, 'not enough values to unpack'):
      _ = run_deeptrio.create_all_commands('/tmp/deeptrio_tmp_output')

  @parameterized.parameters(
      ('batch_size=1024', '--batch_size "1024"'),
      ('batch_size=4096,'
       'config_string="gpu_options: {per_process_gpu_memory_fraction: 0.5}"',
       '--batch_size "4096" '
       '--config_string "gpu_options: {per_process_gpu_memory_fraction: 0.5}"'),
  )
  @flagsaver.FlagSaver
  def test_call_variants_extra_args(self, call_variants_extra_args,
                                    expected_args):
    FLAGS.model_type = 'WGS'
    FLAGS.ref = 'your_ref'
    FLAGS.sample_name_child = 'your_sample_child'
    FLAGS.sample_name_parent1 = 'your_sample_parent1'
    FLAGS.sample_name_parent2 = 'your_sample_parent2'
    FLAGS.reads_child = 'your_bam_child'
    FLAGS.reads_parent1 = 'your_bam_parent1'
    FLAGS.reads_parent2 = 'your_bam_parent2'
    FLAGS.output_vcf_child = 'your_vcf_child'
    FLAGS.output_vcf_parent1 = 'your_vcf_parent1'
    FLAGS.output_vcf_parent2 = 'your_vcf_parent2'
    FLAGS.output_gvcf_child = 'your_gvcf_child'
    FLAGS.output_gvcf_parent1 = 'your_gvcf_parent1'
    FLAGS.output_gvcf_parent2 = 'your_gvcf_parent2'
    FLAGS.num_shards = 64
    FLAGS.call_variants_extra_args = call_variants_extra_args
    commands = run_deeptrio.create_all_commands('/tmp/deeptrio_tmp_output')

    self.assertEqual(
        commands[1], 'time /opt/deepvariant/bin/call_variants '
        '--outfile '
        '"/tmp/deeptrio_tmp_output/call_variants_output_child.tfrecord.gz" '
        '--examples "/tmp/deeptrio_tmp_output/make_examples_child.tfrecord@64.gz" '
        '--checkpoint "/opt/models/deeptrio/wgs/child/model.ckpt" '
        '%s' % expected_args)

  @parameterized.parameters(
      ('qual_filter=3.0', '--qual_filter "3.0"'),)
  @flagsaver.FlagSaver
  def test_postprocess_variants_extra_args(self,
                                           postprocess_variants_extra_args,
                                           expected_args):
    FLAGS.model_type = 'WGS'
    FLAGS.ref = 'your_ref'
    FLAGS.sample_name_child = 'your_sample_child'
    FLAGS.sample_name_parent1 = 'your_sample_parent1'
    FLAGS.sample_name_parent2 = 'your_sample_parent2'
    FLAGS.reads_child = 'your_bam_child'
    FLAGS.reads_parent1 = 'your_bam_parent1'
    FLAGS.reads_parent2 = 'your_bam_parent2'
    FLAGS.output_vcf_child = 'your_vcf_child'
    FLAGS.output_vcf_parent1 = 'your_vcf_parent1'
    FLAGS.output_vcf_parent2 = 'your_vcf_parent2'
    FLAGS.output_gvcf_child = 'your_gvcf_child'
    FLAGS.output_gvcf_parent1 = 'your_gvcf_parent1'
    FLAGS.output_gvcf_parent2 = 'your_gvcf_parent2'
    FLAGS.num_shards = 64
    FLAGS.postprocess_variants_extra_args = postprocess_variants_extra_args
    commands = run_deeptrio.create_all_commands('/tmp/deeptrio_tmp_output')

    self.assertEqual(
        commands[4], 'time /opt/deepvariant/bin/postprocess_variants '
        '--ref "your_ref" '
        '--infile '
        '"/tmp/deeptrio_tmp_output/call_variants_output_child.tfrecord.gz" '
        '--outfile "your_vcf_child" '
        '--nonvariant_site_tfrecord_path '
        '"/tmp/deeptrio_tmp_output/gvcf_child.tfrecord@64.gz" '
        '--gvcf_outfile "your_gvcf_child" '
        '%s' % expected_args)

  @parameterized.parameters(
      (True, 'vcf_stats_report=true', '--vcf_stats_report'),
      (True, 'vcf_stats_report=false', '--novcf_stats_report'),
      # These two cases demonstrate we might end up havig duplicated and
      # potentially conflicting flags when using *extra_args.
      (False, 'vcf_stats_report=true', '--novcf_stats_report --vcf_stats_report'
      ),
      (False, 'vcf_stats_report=false',
       '--novcf_stats_report --novcf_stats_report'),
  )
  @flagsaver.FlagSaver
  def test_postprocess_variants_duplicate_extra_args(
      self, vcf_stats_report, postprocess_variants_extra_args,
      expected_vcf_stats_report):
    FLAGS.model_type = 'WGS'
    FLAGS.ref = 'your_ref'
    FLAGS.sample_name_child = 'your_sample_child'
    FLAGS.sample_name_parent1 = 'your_sample_parent1'
    FLAGS.sample_name_parent2 = 'your_sample_parent2'
    FLAGS.reads_child = 'your_bam_child'
    FLAGS.reads_parent1 = 'your_bam_parent1'
    FLAGS.reads_parent2 = 'your_bam_parent2'
    FLAGS.output_vcf_child = 'your_vcf_child'
    FLAGS.output_vcf_parent1 = 'your_vcf_parent1'
    FLAGS.output_vcf_parent2 = 'your_vcf_parent2'
    FLAGS.output_gvcf_child = 'your_gvcf_child'
    FLAGS.output_gvcf_parent1 = 'your_gvcf_parent1'
    FLAGS.output_gvcf_parent2 = 'your_gvcf_parent2'
    FLAGS.num_shards = 64
    FLAGS.vcf_stats_report = vcf_stats_report
    FLAGS.postprocess_variants_extra_args = postprocess_variants_extra_args
    commands = run_deeptrio.create_all_commands('/tmp/deeptrio_tmp_output')

    self.assertEqual(
        commands[4], 'time /opt/deepvariant/bin/postprocess_variants '
        '--ref "your_ref" '
        '--infile '
        '"/tmp/deeptrio_tmp_output/call_variants_output_child.tfrecord.gz" '
        '--outfile "your_vcf_child" '
        '--nonvariant_site_tfrecord_path '
        '"/tmp/deeptrio_tmp_output/gvcf_child.tfrecord@64.gz" '
        '--gvcf_outfile "your_gvcf_child" '
        '%s' % expected_vcf_stats_report)

if __name__ == '__main__':
  absltest.main()
