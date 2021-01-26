from busco.analysis.BuscoAnalysis import BuscoAnalysis
from busco.analysis.GenomeAnalysis import (
    GenomeAnalysisEukaryotesAugustus,
    GenomeAnalysisEukaryotesMetaeuk,
)
from busco.analysis.TranscriptomeAnalysis import (
    TranscriptomeAnalysisProkaryotes,
    TranscriptomeAnalysisEukaryotes,
)
from busco.analysis.GeneSetAnalysis import GeneSetAnalysis
from busco.analysis.GenomeAnalysis import GenomeAnalysisProkaryotes
from busco.BuscoLogger import BuscoLogger
from busco.BuscoConfig import BuscoConfigMain
from busco.busco_tools.base import NoGenesError, BaseRunner
from configparser import NoOptionError
import os
import shutil

logger = BuscoLogger.get_logger(__name__)


class BuscoRunner:

    mode_dict = {
        "euk_genome_met": GenomeAnalysisEukaryotesMetaeuk,
        "euk_genome_aug": GenomeAnalysisEukaryotesAugustus,
        "prok_genome": GenomeAnalysisProkaryotes,
        "euk_tran": TranscriptomeAnalysisEukaryotes,
        "prok_tran": TranscriptomeAnalysisProkaryotes,
        "proteins": GeneSetAnalysis,
    }

    final_results = []
    results_datasets = []

    def __init__(self, config):

        self.config = config
        setattr(BaseRunner, "config", config)
        setattr(BuscoAnalysis, "config", config)

        self.mode = self.config.get("busco_run", "mode")
        self.domain = self.config.get("busco_run", "domain")

        if self.mode == "genome":
            if self.domain in ["prokaryota", "viruses"]:
                self.mode = "prok_genome"
            elif self.domain == "eukaryota":
                if self.config.getboolean("busco_run", "use_augustus"):
                    self.mode = "euk_genome_aug"
                else:
                    self.mode = "euk_genome_met"
            else:
                raise SystemExit("Unrecognized mode {}".format(self.mode))

        elif self.mode == "transcriptome":
            if self.domain == "prokaryota":
                self.mode = "prok_tran"
            elif self.domain == "eukaryota":
                self.mode = "euk_tran"
            else:
                raise SystemExit("Unrecognized mode {}".format(self.mode))
        analysis_type = type(self).mode_dict[self.mode]
        self.analysis = analysis_type()
        self.prok_fail_count = (
            0  # Needed to check if both bacteria and archaea return no genes.
        )
        self.cleaned_up = False

    def run_analysis(self, callback=(lambda *args: None)):
        try:
            self.analysis.run_analysis()
            s_buscos = self.analysis.hmmer_runner.single_copy
            d_buscos = self.analysis.hmmer_runner.multi_copy
            f_buscos = self.analysis.hmmer_runner.only_fragments
            s_percent = self.analysis.hmmer_runner.s_percent
            d_percent = self.analysis.hmmer_runner.d_percent
            f_percent = self.analysis.hmmer_runner.f_percent
            self.cleanup()

        except NoGenesError as nge:
            no_genes_msg = (
                "{0} did not recognize any genes matching the dataset {1} in the input file. "
                "If this is unexpected, check your input file and your "
                "installation of {0}\n".format(
                    nge.gene_predictor, self.analysis._lineage_name
                )
            )
            fatal = (
                isinstance(self.config, BuscoConfigMain)
                or (
                    self.config.getboolean("busco_run", "auto-lineage-euk")
                    and self.mode == "euk_genome"
                )
                or (
                    self.config.getboolean("busco_run", "auto-lineage-prok")
                    and self.mode == "prok_genome"
                )
                and self.prok_fail_count == 1
            )
            if fatal:
                raise SystemExit(no_genes_msg)
            else:
                logger.warning(no_genes_msg)
                s_buscos = d_buscos = f_buscos = s_percent = d_percent = f_percent = 0.0
                if self.mode == "prok_genome":
                    self.prok_fail_count += 1

        except SystemExit as se:
            self.cleanup()

            raise se
        return callback(s_buscos, d_buscos, f_buscos, s_percent, d_percent, f_percent)

    def cleanup(self):
        self.analysis.cleanup()
        self.cleaned_up = True

    def format_results(self):
        framed_output = []
        if len(type(self).results_datasets) == 1:
            header1 = "Results from dataset {}\n".format(type(self).results_datasets[0])
        else:
            header1 = "Results from generic domain {}\n".format(
                type(self).results_datasets[0]
            )
        final_output_results1 = "".join(
            self._check_parasitic(type(self).final_results[0][1:])
        )
        sb1 = SmartBox()
        framed_lines1 = sb1.create_results_box(header1, final_output_results1)
        framed_output.append(framed_lines1)

        if len(type(self).final_results) == 2:
            header2 = "Results from dataset {}\n".format(type(self).results_datasets[1])
            final_output_results2 = "".join(
                self._check_parasitic(type(self).final_results[1][1:])
            )
            sb2 = SmartBox()
            framed_lines2 = sb2.create_results_box(header2, final_output_results2)
            framed_output.append(framed_lines2)

        return "".join(framed_output)

    def _check_parasitic(self, final_output_results):
        try:
            with open(
                os.path.join(self.analysis._lineage_dataset, "missing_in_parasitic.txt")
            ) as parasitic_file:
                missing_in_parasitic_buscos = [
                    entry.strip() for entry in parasitic_file.readlines()
                ]
            if (
                len(self.analysis.hmmer_runner.missing_buscos)
                >= 0.8 * len(missing_in_parasitic_buscos)
                and len(missing_in_parasitic_buscos) > 0
            ):
                intersection = [
                    mb
                    for mb in self.analysis.hmmer_runner.missing_buscos
                    if mb in missing_in_parasitic_buscos
                ]
                percent_missing_in_parasites = round(
                    100
                    * len(intersection)
                    / len(self.analysis.hmmer_runner.missing_buscos),
                    1,
                )
                if percent_missing_in_parasites >= 80.0:
                    corrected_summary = self._recalculate_parasitic_scores(
                        len(missing_in_parasitic_buscos)
                    )
                    positive_parasitic_line = (
                        "\n!!! The missing BUSCOs match the pattern of a parasitic-reduced "
                        "genome. {}% of your missing BUSCOs are typically missing in these. "
                        "A corrected score would be: \n{}\n".format(
                            percent_missing_in_parasites, corrected_summary
                        )
                    )
                    final_output_results.append(positive_parasitic_line)
                    if not self.config.getboolean("busco_run", "auto-lineage"):
                        auto_lineage_line = "\nConsider using the auto-lineage mode to select a more specific lineage."
                        final_output_results.append(auto_lineage_line)

        except OSError:
            pass

        return final_output_results

    def _recalculate_parasitic_scores(self, num_missing_in_parasitic):
        total_buscos = (
            self.analysis.hmmer_runner.total_buscos - num_missing_in_parasitic
        )
        single_copy = self.analysis.hmmer_runner.single_copy
        multi_copy = self.analysis.hmmer_runner.multi_copy
        fragmented_copy = self.analysis.hmmer_runner.only_fragments
        s_percent = abs(round(100 * single_copy / total_buscos, 1))
        d_percent = abs(round(100 * multi_copy / total_buscos, 1))
        f_percent = abs(round(100 * fragmented_copy / total_buscos, 1))

        one_line_summary = "C:{}%[S:{}%,D:{}%],F:{}%,M:{}%,n:{}\t\n".format(
            round(s_percent + d_percent, 1),
            s_percent,
            d_percent,
            f_percent,
            round(100 - s_percent - d_percent - f_percent, 1),
            total_buscos,
        )
        return one_line_summary

    def organize_final_output(self):
        main_out_folder = self.config.get("busco_run", "main_out")

        try:
            domain_results_folder = self.config.get("busco_run", "domain_run_name")
            root_domain_output_folder = os.path.join(
                main_out_folder, "auto_lineage", "run_{}".format(domain_results_folder)
            )
            root_domain_output_folder_final = os.path.join(
                main_out_folder, "run_{}".format(domain_results_folder)
            )
            os.rename(root_domain_output_folder, root_domain_output_folder_final)
            os.symlink(root_domain_output_folder_final, root_domain_output_folder)
            shutil.copyfile(
                os.path.join(root_domain_output_folder_final, "short_summary.txt"),
                os.path.join(
                    main_out_folder,
                    "short_summary.generic.{}.{}.txt".format(
                        domain_results_folder.replace("run_", ""),
                        os.path.basename(main_out_folder),
                    ),
                ),
            )

        except NoOptionError:
            pass

        except OSError:
            pass

        finally:
            lineage_results_folder = self.config.get("busco_run", "lineage_results_dir")
            lineage_results_path = os.path.join(main_out_folder, lineage_results_folder)
            shutil.copyfile(
                os.path.join(lineage_results_path, "short_summary.txt"),
                os.path.join(
                    main_out_folder,
                    "short_summary.specific.{}.{}.txt".format(
                        lineage_results_folder.replace("run_", ""),
                        os.path.basename(main_out_folder),
                    ),
                ),
            )
        return

    @staticmethod
    def move_log_file(config):
        # This is deliberately a staticmethod so it can be called from run_BUSCO() even if BuscoRunner has not yet
        # been initialized.
        try:
            log_folder = os.path.join(config.get("busco_run", "main_out"), "logs")
            if not os.path.exists(log_folder):
                os.makedirs(log_folder)
            shutil.move(
                "busco_{}.log".format(BuscoLogger.random_id),
                os.path.join(log_folder, "busco.log"),
            )
        except OSError:
            logger.warning(
                "Unable to move 'busco_{}.log' to the 'logs' folder.".format(
                    BuscoLogger.random_id
                )
            )
        return

    def finish(self, elapsed_time):

        final_output_results = self.format_results()
        logger.info("".join(final_output_results))

        self.organize_final_output()

        if not logger.has_warning():
            logger.info(
                "BUSCO analysis done. Total running time: {} seconds".format(
                    str(round(elapsed_time))
                )
            )
        else:
            logger.info(
                "BUSCO analysis done with WARNING(s). Total running time: {} seconds\n\n"
                "***** Summary of warnings: *****".format(str(round(elapsed_time)))
            )
            for item in type(logger).warn_output.getvalue().split("\n"):
                print(item)

        logger.info("Results written in {}".format(self.analysis.main_out))
        logger.info(
            "For assistance with interpreting the results, please consult the userguide: "
            "https://busco.ezlab.org/busco_userguide.html\n"
        )

        self.move_log_file(self.config)


class SmartBox:
    def __init__(self):
        self.width = None

    def wrap_header(self, header_text):
        if len(header_text) < 80:
            self.width = max(50, len(header_text.expandtabs()))
        else:
            self.width = 50
            header_text = self.wrap_long_line(header_text)

        return header_text

    def wrap_long_line(self, line):
        words = line.split(" ")
        word_num = 0
        word_start = 0
        length = 0
        output_lines = []
        while word_num < len(words):
            while length < self.width:
                word_num += 1
                line = " ".join(words[word_start:word_num])
                length = len(line.expandtabs())
                if length >= self.width:
                    word_num -= 1
                    line = " ".join(words[word_start:word_num])
                    break
                if word_num == len(words):
                    break
            output_lines.append(line)
            length = 0
            word_start = word_num
        return "\n".join(output_lines)

    def wrap_text(self, text):
        lines = text.split("\n")
        output_lines = []
        for line in lines:
            line = line.strip()
            if len(line.expandtabs()) < self.width:
                output_lines.append(line)
            else:
                output_lines.append(self.wrap_long_line(line))
        return "\n".join(output_lines)

    def add_vertical(self, lines):
        if isinstance(lines, str):
            lines = lines.strip().split("\n")
        formatted_lines = []
        for line in lines:
            line = "|{}".format(
                line.strip()
            )  # left bar needs to be added before expanding tabs
            whitespace = " " * (self.width - len(line.expandtabs()))
            format_line = "{}{}|".format(line, whitespace)
            formatted_lines.append(format_line)
        return formatted_lines

    def add_horizontal(self):
        return "-" * self.width

    def create_results_box(self, header_text, body_text):
        header = self.wrap_header(header_text)  # Called first to define width
        box_lines = ["\n"]
        box_lines.append("\t{}".format(self.add_horizontal()))
        framed_header = self.add_vertical(header)
        for line in framed_header:
            box_lines.append("\t{}".format(line))
        box_lines.append("\t{}".format(self.add_horizontal()))
        body = self.wrap_text(body_text)
        framed_body = self.add_vertical(body)
        for line in framed_body:
            box_lines.append("\t{}".format(line))
        box_lines.append("\t{}".format(self.add_horizontal()))
        return "\n".join(box_lines)
