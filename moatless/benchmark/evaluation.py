import concurrent.futures
import gc
import json
import logging
import os
import shutil
import threading
import time
import traceback
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional, Any

import litellm
from tqdm.auto import tqdm

from moatless.agent.code_agent import CodingAgent
from moatless.benchmark.report import (
    BenchmarkResult,
    to_dataframe,
    create_sha256_hash,
)
from moatless.benchmark.swebench import (
    load_instance,
    create_workspace,
)
from moatless.settings import TreeSearchSettings, Settings, ModelSettings
from moatless.search_tree import SearchTree
from moatless.value_function import ValueFunction

logger = logging.getLogger(__name__)


class Evaluation:
    def __init__(
        self,
        evaluations_dir: str,
        evaluation_name: str,
        settings: TreeSearchSettings,
        use_perfect_file_context: bool = False,
        max_file_context_tokens: int = 16000,
        dataset_name: str = "princeton-nlp/SWE-bench_Lite",
        repo_base_dir: str | None = None,
        report_mode: str | None = None,
        litellm_callback: Optional[str] = None,
        num_workers: int = 1,
        use_testbed: bool = False,
        use_local_git_upstream: bool = True,
        **kwargs,
    ):
        self.evaluations_dir = evaluations_dir
        self.num_workers = num_workers
        self.report_mode = report_mode
        self.dataset_name = dataset_name
        self.evaluation_name = evaluation_name
        self.use_local_git_upstream = use_local_git_upstream

        self.use_testbed = use_testbed

        self.settings = settings
        self.use_perfect_file_context = use_perfect_file_context
        self.max_file_context_tokens = max_file_context_tokens

        self.evaluation_dir = f"{evaluations_dir}/{evaluation_name}"
        logger.info(f"Evaluation directory: {self.evaluation_dir}")
        if not os.path.exists(self.evaluation_dir):
            os.makedirs(self.evaluation_dir)

        self.predictions_path = f"{self.evaluation_dir}/all_preds.jsonl"

        self.repo_base_dir = repo_base_dir or os.getenv("REPO_DIR", "/tmp/repos")

        if litellm_callback:
            litellm.success_callback = [litellm_callback]
            litellm.failure_callback = [litellm_callback]

        self.status_file = f"{self.evaluation_dir}/status_summary.json"
        self.event_file = f"{self.evaluation_dir}/event_log.json"
        self.file_lock = threading.Lock()
        self.statuses = defaultdict(dict)
        self.events = defaultdict(list)

    def update_status(self, instance_id: str, status: str):
        with self.file_lock:
            if instance_id not in self.statuses:
                self.statuses[instance_id] = {
                    "created": datetime.now().isoformat(),
                }

            self.statuses[instance_id].update(
                {"last_updated": datetime.now().isoformat(), "status": status}
            )
            self._save_statuses()

    def log_event(self, instance_id: str, event: str):
        with self.file_lock:
            self.events[instance_id].append(
                {"timestamp": datetime.now().isoformat(), "event": event}
            )
            self._save_events()

    def _save_statuses(self):
        with open(self.status_file, "w") as f:
            json.dump(self.statuses, f, indent=2)

    def _save_events(self):
        with open(self.event_file, "w") as f:
            json.dump(self.events, f, indent=2)

    def run_evaluation(
        self,
        split: str = "lite",
        resolved_by: Optional[int] = None,
        instance_ids: list[str] | None = None,
        ignore_repos: list[str] | None = None,
    ):
        file_path = os.path.join(
            os.path.dirname(__file__), f"swebench_{split}_all_evaluations.json"
        )
        with open(file_path) as f:
            instances = json.load(f)

        instances = sorted(instances, key=lambda x: len(x["resolved_by"]), reverse=True)
        logger.info(f"Loaded {len(instances)} instances from {file_path}")

        if instance_ids:
            instances = [
                instance
                for instance in instances
                if instance["instance_id"] in instance_ids
            ]

            logger.info(
                f"Running evaluation for {len(instances)} instances filtered by instance_ids"
            )

        if resolved_by:
            instances = [
                instance
                for instance in instances
                if len(instance["resolved_by"]) >= resolved_by
                or (
                    resolved_by == 1
                    and instance.get("llm_monkeys", {}).get("resolved_rate", 0) > 0
                )
            ]

            logger.info(
                f"Running evaluation for {len(instances)} instances filtered by resolved_by >= {resolved_by}"
            )

        if ignore_repos:
            instances = [
                instance
                for instance in instances
                if instance["repo"] not in ignore_repos
            ]

            if instances:
                logger.info(
                    f"Running evaluation for {len(instances)} instances after filtering by ignore_repos"
                )

        return self._run_evaluation(instances)

    def run_single_instance(
        self,
        instance_id: str,
        dataset: str = "princeton-nlp/SWE-bench_Lite",
        split="test",
    ) -> BenchmarkResult:
        instance = load_instance(instance_id, dataset, split)
        return self.evaluate_instance(instance)

    def evaluate_instance(self, instance: dict):
        instance_id = instance["instance_id"]
        instance_dir = os.path.join(self.evaluation_dir, f"{instance_id}")
        trajectory_path = os.path.join(instance_dir, "trajectory.json")

        if not os.path.exists(self.evaluation_dir):
            os.makedirs(trajectory_path)

        log_dir = os.path.join(instance_dir, "logs")
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)

        eval_result_path = os.path.join(instance_dir, "eval_result.json")
        if os.path.exists(eval_result_path):
            with open(eval_result_path) as f:
                eval_result = json.load(f)
        else:
            eval_result = {
                "node_results": {},
            }

        logger.info(f"Evaluating {instance_id}")
        problem_statement = instance["problem_statement"]

        workspace = None
        testbed = None

        self.update_status(instance_id, "started")
        self.log_event(instance_id, "evaluate_instance_initiated")

        try:
            search_tree = None

            workspace = create_workspace(
                instance,
                repo_base_dir=self.repo_base_dir,
                use_perfect_file_context=self.use_perfect_file_context,
                max_file_context_tokens=self.max_file_context_tokens,
                use_testbed=self.use_testbed,
                log_dir=log_dir,
            )
            if os.path.exists(trajectory_path):
                persisted_tree = SearchTree.from_file(trajectory_path)
                if persisted_tree.is_finished():
                    logger.info(f"Found completed search tree for {instance_id}")
                    search_tree = persisted_tree

            if not search_tree:
                self.log_event(instance_id, "workspace_creation_started")

                self.log_event(instance_id, "workspace_created")

                metadata: dict[str, Any] = {
                    "evaluation_name": self.evaluation_name,
                    "instance_id": instance["instance_id"],
                }

                if os.path.exists(trajectory_path):
                    search_tree = SearchTree.from_file(trajectory_path, workspace)
                else:

                    instance = load_instance("django__django-16379")
                    workspace = create_workspace(instance)

                    agent = CodingAgent(workspace=workspace, model_settings=self.settings.agent_model)
                    value_function = ValueFunction(model_settings=self.settings.value_function_model)

                    search_tree = SearchTree(
                        message=problem_statement,
                        workspace=workspace,
                        agent=agent,
                        value_function=value_function,
                        settings=self.settings,
                        metadata=metadata,
                        persist_path=trajectory_path,
                    )

            best_node = None
            start_time = time.time()
            try:
                self.log_event(instance_id, "search_tree_execution_started")
                search_tree.run_search()
                best_node = search_tree.get_best_trajectory()
                self.log_event(instance_id, "search_tree_execution_completed")
                eval_result["status"] = "completed"
            except Exception:
                eval_result["error"] = traceback.format_exc()
                eval_result["status"] = "error"
                logging.exception(f"Error in evaluation of {instance['instance_id']} ")
            finally:
                eval_result["duration"] = time.time() - start_time
                search_tree.persist(trajectory_path)

            finished_nodes = search_tree.get_finished_nodes()
            patch_results = {}
            logger.info(
                f"Will evaluate {len(finished_nodes)} finished nodes for instance {instance_id}"
            )

            if "node_results" not in eval_result:
                eval_result["node_results"] = {}

            if self.use_testbed and workspace and workspace.runtime:
                for i, finished_node in enumerate(finished_nodes):
                    logger.info(
                        f"Evaluate finished Node{finished_node.node_id} {i+1}/{len(finished_nodes)} for instance {instance_id}"
                    )

                    if finished_node.node_id in eval_result["node_results"]:
                        continue

                    patch = finished_node.file_context.generate_git_patch()
                    patch_hash = create_sha256_hash(patch)

                    if patch:
                        if patch_hash in patch_results:
                            logger.info(
                                f"Use already evaluated patch for Node{finished_node.node_id} in {instance_id}"
                            )
                            eval_result["node_results"][finished_node.node_id] = (
                                patch_results[patch_hash]
                            )
                        else:
                            start_time = time.time()
                            result = workspace.runtime.evaluate(patch=patch)
                            if not result:
                                logger.error(
                                    f"Error in evaluating patch for {instance_id}"
                                )
                                continue

                            eval_result["node_results"][finished_node.node_id] = (
                                result.model_dump()
                            )
                            patch_results[patch_hash] = result.model_dump()
                            logger.info(
                                f"Evaluated patch in {time.time() - start_time} seconds (resolved: {result.resolved})"
                            )

                    if best_node and finished_node.node_id == best_node.node_id:
                        self.save_prediction(instance_id, patch)
                        eval_result["selected_node"] = finished_node.node_id

                        if eval_result["node_results"].get(finished_node.node_id):
                            eval_result["resolved"] = eval_result["node_results"][
                                finished_node.node_id
                            ]["resolved"]

                            if eval_result.get("resolved"):
                                logger.info(f"Resolved {instance['instance_id']}")
                            else:
                                logger.info(
                                    f"Could not resolve {instance['instance_id']}"
                                )

                    with open(eval_result_path, "w") as f:
                        json.dump(eval_result, f, indent=2)

            self.log_event(instance_id, "evaluation_completed")
            self.update_status(instance_id, eval_result["status"])

            return eval_result

        except Exception:
            logger.exception(f"Error in processing instance {instance_id}")
            self.log_event(instance_id, "evaluation_error")
            self.update_status(instance_id, "error")
            return None

        finally:
            with open(eval_result_path, "w") as f:
                json.dump(eval_result, f, indent=2)
            # Clean up
            if workspace and workspace.file_repo:
                shutil.rmtree(workspace.file_repo.repo_dir, ignore_errors=True)
                if workspace.runtime and workspace.runtime.testbed:
                    try:
                        workspace.runtime.testbed.destroy()
                    except Exception:
                        logger.exception("Error deleting testbed")

            del workspace
            del search_tree
            # del result
            gc.collect()

    def save_prediction(self, instance_id, submission):
        with self.file_lock:
            prediction = {
                "model_name_or_path": self.evaluation_name,
                "instance_id": instance_id,
                "model_patch": submission,
            }
            with open(self.predictions_path, "a") as file:
                json_string = json.dumps(prediction)
                file.write(json_string + "\n")

    def _to_csv_report(self, results: list[BenchmarkResult]):
        df = to_dataframe(results, self.report_mode)
        df.to_csv(
            f"{self.evaluation_dir}/result.csv",
            index=False,
            sep=",",
            decimal=",",
            quoting=1,
        )

    def _run_evaluation(self, instances: list[dict]):
        error = 0

        with open(self.predictions_path, "w") as file:
            file.write("")

        results = []

        logger.info(
            f"Processing {len(instances)} instances with {self.num_workers} workers"
        )

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self.num_workers
        ) as executor:
            futures = [
                executor.submit(self.evaluate_instance, instance)
                for instance in instances
            ]

            pbar = tqdm(concurrent.futures.as_completed(futures), total=len(futures))

            for future in pbar:
                try:
                    result = future.result()
                    # TODO
                    # if result:
                    #    results.append(result)
                    #    # self._to_csv_report(results)
                    #    self._save_json_report(results)
                    # else:
                    #    error += 1

                    # stats = self._create_stats(results)
                    # pbar.set_postfix(stats)

                except Exception:
                    error += 1
                    logger.exception("Error in processing instance")

        logger.info(f"Completed processing with {error} errors")
        self.update_status("all", "evaluation_completed")

    def _create_stats(self, results):
        stats = {}
        if results:
            stats["avg_duration"] = sum(r.duration for r in results) / len(results)
            stats["avg_cost"] = sum(r.total_cost for r in results) / len(results)
            stats["total_cost"] = sum(r.total_cost for r in results)

            identified = sum(
                1
                for r in results
                if r.status in ["identified", "planned", "edited", "resolved"]
            )
            resolved = sum(1 for r in results if r.status in ["resolved"])
            error = sum(1 for r in results if r.status == "error")

            if identified > 0:
                stats["identified"] = f"{(identified / len(results)) * 100:.2f}%"
            if resolved > 0:
                stats["resolved"] = f"{(resolved / len(results)) * 100:.2f}%"
            stats["error"] = error

        return stats

    def _save_json_report(self, results: list[BenchmarkResult]):
        json_results = [result.model_dump() for result in results]
        with open(f"{self.evaluation_dir}/report.json", "w") as f:
            json.dump(json_results, f, indent=2)

    def read_trajectory(self, path) -> dict | None:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        else:
            return None

    def get_actions(self, trajectory: dict):
        actions = []
        for transition in trajectory["transitions"]:
            for action in transition["actions"]:
                actions.append(action["action"])
        return actions


def create_evaluation_name(
    model: str,
    date,
    max_expansions=None,
    **kwargs,
):
    if date:
        date_str = date
    else:
        date_str = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
    model_name = model.split("/")[-1]
    model_name = f"{date_str}_{model_name}"
    if max_expansions:
        model_name += f"_max_exp{max_expansions}"
    for key, value in kwargs.items():
        model_name += f"_{key}_{value}"
    return model_name
