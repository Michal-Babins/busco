# coding: utf-8
"""
AutoLineage.py

Integration of Auto-Select Lineage pipeline.

Author(s): Matthew Berkeley, Mathieu Seppey, Mose Manni

Copyright (c) 2015-2024, Evgeny Zdobnov (ez@ezlab.org). All rights reserved.

License: Licensed under the MIT license. See LICENSE.md file.

"""

import os
from busco.BuscoPlacer import BuscoPlacer
from busco.BuscoLogger import BuscoLogger
from busco.BuscoLogger import LogDecorator as log
from busco.BuscoRunner import AnalysisRunner
from busco.Exceptions import BuscoError
import numpy as np

logger = BuscoLogger.get_logger(__name__)


class EmptyResultsError(BuscoError):
    def __init__(self):
        super().__init__(
            "No genes were recognized by BUSCO. Please check the content of your input file."
        )


class AutoSelectLineage:
    """
    Class for selecting the best lineage dataset for the input data.
    Auto Selector works by running BUSCO using all available datasets and identifying the dataset that returns the
    highest BUSCO score.
    """

    runners = []
    root_dataset_versions = []

    @log(
        "***** Starting Auto Select Lineage *****\n\t"
        "This process runs BUSCO on the generic lineage datasets for the domains archaea, bacteria and eukaryota. "
        "Once the optimal domain is selected, BUSCO automatically attempts to find the most appropriate BUSCO dataset "
        "to use based on phylogenetic placement.\n\t"
        "--auto-lineage-euk and --auto-lineage-prok are also available if you know your input assembly is, or is not, "
        "an eukaryote. See the user guide for more information.\n\tA reminder: Busco evaluations are valid when an "
        "appropriate dataset is used, i.e., the dataset belongs to the lineage of the species to test. "
        "Because of overlapping markers/spurious matches among domains, busco matches in another domain do not "
        "necessarily mean that your genome/proteome contains sequences from this domain. "
        "However, a high busco score in multiple domains might help you identify possible contaminations.",
        logger,
    )
    def __init__(self, config_manager):
        self.config_manager = config_manager
        self.config = self.config_manager.config_main
        if self.config.getboolean("busco_run", "auto-lineage-prok"):
            self.all_lineages = ["archaea", "bacteria"]
        elif self.config.getboolean("busco_run", "auto-lineage-euk"):
            self.all_lineages = ["eukaryota"]
        else:
            self.all_lineages = ["archaea", "bacteria", "eukaryota"]
        self.dataset_version = self.config.get("busco_run", "datasets_version")
        self.callback = self.record_results
        self.s_buscos = []
        self.d_buscos = []
        self.f_buscos = []
        self.s_percents = []
        self.d_percents = []
        self.f_percents = []
        self.best_match_lineage_dataset = None
        self.current_lineage = None
        self.virus_pipeline = False
        self.selected_runner = None

    def record_results(
        self, s_buscos, d_buscos, f_buscos, s_percent, d_percent, f_percent
    ):
        """
        Record results of BUSCO run.

        :param float s_buscos: Number of Single copy BUSCOs present in data
        :param float d_buscos: Number of Double copy BUSCOs present in data
        :param float f_buscos: Number of Fragmented BUSCOs present in data
        :param float s_percent: Percentage of Single copy BUSCOs present in data
        :param float d_percent: Percentage of Double copy BUSCOs present in data
        :param float f_percent: Percentage of Fragmented BUSCOs present in data
        :return: None
        """
        self.s_buscos.append(s_buscos)
        self.d_buscos.append(d_buscos)
        self.f_buscos.append(f_buscos)
        self.s_percents.append(s_percent)
        self.d_percents.append(d_percent)
        self.f_percents.append(f_percent)
        return

    @classmethod
    def reset(cls):
        cls.runners = []

    @log("Running auto selector", logger, debug=True)
    def run_auto_selector(self):
        """
        Run BUSCO on each lineage listed in all_lineages.
        :return:
        """
        odb_versions = self.check_odb_versions(self.all_lineages)
        type(self).root_dataset_versions = ["{}_{}".format(self.all_lineages[i], odb_versions[i]) for i in range(len(self.all_lineages))]
        root_runners = self.run_lineages_list(self.all_lineages, versions=odb_versions)
        self.get_best_match_lineage(root_runners)
        if self.virus_check() and not self.config.getboolean(
            "busco_run", "auto-lineage-euk"
        ):
            self.virus_pipeline = True
            self.run_virus_datasets()
            self.get_best_match_lineage(type(self).runners)

        if (
            len(self.selected_runner.analysis.hmmer_runner.single_copy_buscos)
            == len(self.selected_runner.analysis.hmmer_runner.multi_copy_buscos)
            == len(self.selected_runner.analysis.hmmer_runner.fragmented_buscos)
            == 0
        ):
            raise EmptyResultsError

        logger.info(
            "{} selected\n".format(os.path.basename(self.best_match_lineage_dataset))
        )
        return

    def check_odb_versions(self, lineages):
        odb_versions = []
        for lineage in lineages:
            dataset_name = "{}_{}".format(lineage, self.dataset_version)
            files_to_check = [
                dataset_name,
                "list_of_reference_markers.{}.txt".format(dataset_name),
                "mapping_taxid-lineage.{}.txt".format(dataset_name),
                "mapping_taxids-busco_dataset_name.{}.txt".format(dataset_name),
                "supermatrix.aln.{}.faa".format(dataset_name),
                "tree.{}.nwk".format(dataset_name),
                "tree_metadata.{}.txt".format(dataset_name),
            ]
            for filename in files_to_check:
                try:
                    self.config.downloader.check_latest_version(filename)
                except BuscoError as e:
                    if "not a valid download option" in str(e):
                        logger.warning(
                            "The auto-lineage pipeline is not yet available using the parent dataset {}. Reverting to {}.".format(
                                dataset_name, dataset_name.split("_odb")[0] + "_odb10"
                            )
                        )

                        odb_versions.append("odb10")
                        break
            else:
                odb_versions.append(self.dataset_version)
        return odb_versions

    def virus_check(self):
        return (self.selected_runner.analysis.hmmer_runner.s_percent < 3.0) & (
            os.stat(self.selected_runner.analysis.input_file).st_size < 500000
        )

    @log("Running virus detection pipeline", logger)
    def run_virus_datasets(self):
        lineages_to_check = []
        virus_datasets = self.selected_runner.config.downloader.get(
            "virus_datasets.txt", "information"
        )
        with open(virus_datasets, "r") as vir_sets:
            for line in vir_sets:
                lineages_to_check.append(line.strip().split("_odb")[0])
        self.run_lineages_list(lineages_to_check)
        return

    def run_lineages_list(self, lineages_list, versions=None):
        root_runners = []
        for idx, l in enumerate(lineages_list):
            version = self.dataset_version if not versions else versions[idx]
            self.current_lineage = "{}_{}".format(l, version)
            try:
                autoconfig = self.config_manager.load_busco_config_auto(
                    self.current_lineage
                )
                autoconfig.set("busco_run", "datasets_version", version)
            except BuscoError as e:
                if "not a valid option" in str(e):
                    self.dataset_version = "odb10"
                    logger.warning(
                        "The lineage dataset {} is not yet available for BUSCO. Reverting to {}.".format(
                            self.current_lineage,
                            self.current_lineage.split("_odb")[0] + "_odb10",
                        )
                    )
                    self.current_lineage = "{}_{}".format(l, self.dataset_version)
                    autoconfig = self.config_manager.load_busco_config_auto(
                        self.current_lineage
                    )
                    autoconfig.set(
                        "busco_run", "datasets_version", self.dataset_version
                    )

            busco_run = AnalysisRunner(autoconfig)
            busco_run.run_analysis(callback=self.callback)
            root_runners.append(busco_run)
            type(self).runners.append(
                busco_run
            )  # Save all root runs so they can be recalled if chosen
            if "genome" in self.config.get("busco_run", "mode"):
                from busco.analysis import (
                    GenomeAnalysis,
                )  # import here because it's a big module to import if not needed

                GenomeAnalysis._bbtools_already_run = True
        return root_runners

    @staticmethod
    def get_max_ind(arr):
        """
        Return maximum ind(s) of array. If max value appears twice, two indices are returned.
        :param arr:
        :return:
        """
        inds = np.arange(len(arr))
        max_mask = arr == np.amax(arr)
        max_ind = inds[max_mask]
        return max_ind

    def evaluate(self, runners, use_percent=False):
        """
        Evaluate output scores from all BUSCO runs. Lineage with the highest number of complete (single + multiple)
        copy BUSCOs is assigned as the best_match_lineage.
        In case of a tie, the number of fragmented BUSCOs are used as a tiebreak.
        I this is still a tie and the number of matched BUSCOs is zero, then an error is raised.
        If there is a further nonzero tie, the tiebreak is the highest percentage of single copy BUSCOs.
        If still a tie, use the first match.
        :return
        """
        self.collate_results(runners)

        max_ind = (
            self.get_max_ind(np.array(self.s_percents) + np.array(self.d_percents))
            if use_percent
            else self.get_max_ind(np.array(self.s_buscos) + np.array(self.d_buscos))
        )
        if len(max_ind) > 1:
            max_ind2 = (
                self.get_max_ind(np.array(self.f_percents)[max_ind])
                if use_percent
                else self.get_max_ind(np.array(self.f_buscos)[max_ind])
            )
            max_ind = max_ind[max_ind2]
            if len(max_ind) > 1:
                if (
                    (self.s_buscos[max_ind[0]] == 0.0)
                    and (self.d_buscos[max_ind[0]] == 0.0)
                    and (self.f_buscos[max_ind[0]] == 0.0)
                ):
                    return int(0)
                else:
                    max_ind3 = self.get_max_ind(np.array(self.s_percents)[max_ind])
                    max_ind = max_ind[max_ind3]
                    if len(max_ind) > 1:
                        logger.warning(
                            "Two lineage runs scored exactly the same. Proceeding with the first."
                        )
                        # I don't expect this error message will ever be used.
                        max_ind = max_ind[0]

        return int(max_ind)

    def collate_results(self, runners):
        self.s_buscos = [runner.analysis.hmmer_runner.single_copy for runner in runners]
        self.d_buscos = [runner.analysis.hmmer_runner.multi_copy for runner in runners]
        self.f_buscos = [
            runner.analysis.hmmer_runner.only_fragments for runner in runners
        ]
        self.s_percents = [runner.analysis.hmmer_runner.s_percent for runner in runners]
        self.d_percents = [runner.analysis.hmmer_runner.d_percent for runner in runners]
        self.f_percents = [runner.analysis.hmmer_runner.f_percent for runner in runners]
        return

    def get_best_match_lineage(
        self, runners, use_percent=False,
    ):
        max_ind = self.evaluate(runners, use_percent)
        self.selected_runner = runners[int(max_ind)]
        self.best_match_lineage_dataset = self.selected_runner.config.get(
            "busco_run", "lineage_dataset"
        )

        self.selected_runner.config.set(
            "busco_run",
            "domain_run_name",
            os.path.basename(self.best_match_lineage_dataset),
        )
        self.selected_runner.config.set(
            "busco_run", "lineage_dataset", self.best_match_lineage_dataset
        )
        self.selected_runner.set_parent_dataset()
        self.dataset_version = self.selected_runner.config.get(
            "busco_run", "datasets_version"
        )
        runners.pop(int(max_ind))
        self.cleanup_disused_runs(runners)
        return

    @staticmethod
    def cleanup_disused_runs(disused_runners):
        for runner in disused_runners:
            if not runner.cleaned_up:
                runner.cleanup()

    def get_lineage_dataset(self):
        """
        Run the output of the auto selection through BuscoPlacer to obtain a more precise lineage dataset.
        :return str lineage_dataset: Local path to the optimal lineage dataset.
        """
        if (
            self.selected_runner.domain == "eukaryota"
            or self.selected_runner.config.getboolean("busco_run", "use_miniprot")
        ):
            self.run_busco_placer()

        elif self.selected_runner.domain == "viruses":
            pass
        else:
            self.run_busco_placer()
        return

    def set_best_match_lineage(self):
        AnalysisRunner.selected_dataset = os.path.basename(
            self.best_match_lineage_dataset
        )

    def run_busco_placer(self):
        if "genome" in self.selected_runner.mode:
            if (
                self.selected_runner.domain == "prokaryota"
                and not self.config.getboolean("busco_run", "use_miniprot")
            ):
                protein_seqs = self.selected_runner.analysis.prodigal_runner.output_faa
            elif self.selected_runner.domain == "eukaryota" or self.config.getboolean(
                "busco_run", "use_miniprot"
            ):
                if self.config.getboolean("busco_run", "use_augustus"):
                    protein_seqs_dir = (
                        self.selected_runner.analysis.augustus_runner.extracted_prot_dir
                    )
                    protein_seqs = [
                        os.path.join(protein_seqs_dir, f)
                        for f in os.listdir(protein_seqs_dir)
                        if f.split(".")[-2] == "faa"
                    ]
                elif self.config.getboolean("busco_run", "use_metaeuk"):  # if metaeuk
                    protein_seqs = (
                        self.selected_runner.analysis.metaeuk_runner.combined_pred_protein_seqs
                    )
                else:  # if miniprot
                    protein_seqs_dir = (
                        self.selected_runner.analysis.miniprot_align_runner.translated_proteins_folder
                    )
                    protein_seqs = [
                        os.path.join(protein_seqs_dir, f)
                        for f in os.listdir(protein_seqs_dir)
                        if f.endswith("faa")
                    ]

        elif "tran" in self.selected_runner.mode:
            if self.selected_runner.mode == "euk_tran":
                protein_seqs = (
                    self.selected_runner.analysis.metaeuk_runner.combined_pred_protein_seqs
                )
            elif self.selected_runner.mode == "prok_tran":
                protein_seqs = self.selected_runner.analysis.single_copy_proteins_file

        else:
            protein_seqs = self.selected_runner.config.get("busco_run", "in")
        out_path = self.config.get("busco_run", "main_out")
        run_folder = os.path.join(
            out_path,
            "auto_lineage",
            self.selected_runner.config.get("busco_run", "lineage_results_dir"),
        )

        bp = BuscoPlacer(
            self.selected_runner.config,
            run_folder,
            protein_seqs,
            self.selected_runner.analysis.hmmer_runner.single_copy_buscos,
        )
        try:
            bp.download_placement_files()
        except BuscoError as e:
            raise BuscoError(
                "Failed to download placement files. Files may not be available for download."
            ) from e
        dataset_details, placement_file_versions = bp.define_dataset()
        self.config.placement_files = placement_file_versions
        # Necessary to pass these filenames to the final run to be recorded.
        lineage, supporting_markers, placed_markers = dataset_details
        self.best_match_lineage_dataset = lineage  # basename
        self.selected_runner.config.set(
            "busco_run",
            "domain_run_name",
            os.path.basename(
                self.selected_runner.config.get("busco_run", "lineage_dataset")
            ),
        )
        self.selected_runner.config.set(
            "busco_run", "lineage_dataset", self.best_match_lineage_dataset
        )
        self.selected_runner.set_parent_dataset()
        return

