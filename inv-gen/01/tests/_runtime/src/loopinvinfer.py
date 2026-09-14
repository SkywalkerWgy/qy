import os
import re
import sys
import time
import argparse
import logging
import signal
import multiprocessing
import framac
import processor


def is_contract_goal_id(goal_id: str) -> bool:
    local_id = goal_id.split(":")[-1]
    return local_id.startswith("e") or local_id.startswith("a")


def compact_text(text, limit=140):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    return text[:limit - 3] + "..."


def artifact_relpath(path, root):
    if not path:
        return ""
    return os.path.relpath(path, root)


def failure_summary(error_msg, limit=6):
    parts = []
    for goal_id, value in error_msg.items():
        if isinstance(value, dict):
            prop = value.get("property") or value.get("status") or "unknown"
            context = value.get("context", "")
            prove = ""
            for line in str(context).splitlines():
                if line.strip().startswith("Prove:"):
                    prove = line.strip()
                    break
            detail = compact_text(prove or context, 120)
            parts.append(f"{goal_id} [{prop}] {detail}".strip())
        else:
            parts.append(f"{goal_id}: {compact_text(value, 120)}")
    if len(parts) > limit:
        hidden = len(parts) - limit
        parts = parts[:limit] + [f"... +{hidden} more"]
    return "; ".join(parts) if parts else "none"


def delayed_filter_threshold(loop_count):
    """Return the failure count at which an invariant is removed.

    The paper version ties delayed filtering to the program structure: an
    invariant candidate is deleted after failing loop_count + 1 times.
    """
    return max(0, int(loop_count)) + 1


class SampleTimeout(Exception):
    pass


def sample_timeout_handler(signum, frame):
    raise SampleTimeout()


def verify(code, file_basename, logpath, logger, maxtime, local_model_context=None,
           ablation_mode=False, source_extension=".c"):
    check = False
    trytime = 1
    proposal = 0
    invalidtime = 0

    # -------------------------------
    # Initialize the loop processor
    # -------------------------------
    loops = processor.build_loops(
        code,
        file_basename,
        model,
        logger,
        local_model_context=local_model_context,
        ablation_mode=ablation_mode,
    )
    filter_threshold = delayed_filter_threshold(len(loops.loop_list))
    logger.info(
        "[INIT] functions=%s loops=%d delayed_filter_threshold=%d",
        ", ".join(region["name"] for region in loops.function_regions),
        len(loops.loop_list),
        filter_threshold,
    )
    if ablation_mode:
        logger.info("[ABLATION] mode=plain initial_prompt=plain_code feedback_prompt_code=without_loop_invariants")
        loops.initial_loop_invariant_inference_ablation()
    else:
        loops.initial_loop_invariant_inference()

    # -------------------------------
    # First phase (bounded attempts)
    # -------------------------------
    while not check and trytime <= maxtime:

        
        loops.logger_inv()
        code_with_inv = loops.inject_invariants()

        out_c_file = os.path.join(logpath, f"{file_basename}_verified_{trytime}{source_extension}")
        with open(out_c_file, "w", encoding="utf-8") as f:
            f.write(code_with_inv)

        proposal += 1
        logger.info(
            "[ATTEMPT %d] phase=bounded candidate=%s invariants=%d",
            trytime,
            artifact_relpath(out_c_file, logpath),
            loops.invariant_count(),
        )

        output_result_type, out_std, out_err, _solve_time = \
            framac.run_framac_with_wp(logpath, os.path.basename(out_c_file))
        logger.info(
            "[FRAMAC] attempt=%d result=%s stdout=%s stderr=%s",
            trytime,
            output_result_type,
            artifact_relpath(out_std, logpath),
            artifact_relpath(out_err, logpath),
        )

        # read WP result
        with open(out_std, 'r') as f:
            log = f.read()

        # ---------- Case: Invalid ----------
        if output_result_type == "Invalid":
            invalid_msg = framac.parse_frama_invalid_output(code_with_inv, log)
            logger.info(
                "[INVALID] attempt=%d remove=%s",
                trytime,
                ", ".join(invalid_msg.keys()) if invalid_msg else "none",
            )
            if not invalid_msg:
                logger.info("[INVALID] attempt=%d no removable invariant found; stop this run", trytime)
                return "", proposal, False
            for k in invalid_msg:
                loops.remove_invariant_by_id(k)
            invalidtime += 1
            if invalidtime > 10:
                return "", proposal, False
            trytime += 1


        # ---------- Case: Fail_k ----------
        elif output_result_type.startswith("Fail_"):
            error_msg = framac.parse_frama_valid_output(log)
            logger.info(
                "[FAIL] attempt=%d failures=%d summary=%s",
                trytime,
                len(error_msg),
                failure_summary(error_msg),
            )

            for k, v in error_msg.items():
                if is_contract_goal_id(k):
                    local_key = k.split(":")[-1]
                    target = loops.ensures.get(k, loops.ensures.get(local_key, str(v)))
                    if isinstance(v, dict):
                        contract_failure = dict(v)
                        contract_failure["target"] = target
                    else:
                        contract_failure = {"target": target, "context": str(v)}
                    loops.revise_loop_invariant_inference(k, contract_failure)
                elif k.startswith("i"):
                    loops.update_error_count(k)
                    if loops.error_count[k] >= filter_threshold:
                        logger.info(
                            "[FILTER] invariant=%s failures=%d threshold=%d action=remove",
                            k,
                            loops.error_count[k],
                            filter_threshold,
                        )
                        loops.remove_invariant_by_id(k)
                        continue
                    loops.revise_loop_invariant_inference(k, v)

            trytime += 1

        # ---------- Case: Pass ----------
        elif output_result_type.startswith("Pass_"):
            logger.info("[PASS] attempt=%d candidate=%s", trytime, artifact_relpath(out_c_file, logpath))
            return code_with_inv, proposal, True

        # ---------- Unknown ----------
        else:
            logger.error(f"Unknown Frama-C result: {output_result_type}")
            return "", proposal, False

    # -------------------------------------------------------------
    # Second phase (aggressive removal / final attempts)
    # -------------------------------------------------------------
    while True:

        loops.logger_inv()
        code_with_inv = loops.inject_invariants()

        out_c_file = os.path.join(logpath, f"{file_basename}_verified_final_attempt_{trytime}{source_extension}")
        with open(out_c_file, "w", encoding="utf-8") as f:
            f.write(code_with_inv)

        proposal += 1
        logger.info(
            "[ATTEMPT %d] phase=final candidate=%s invariants=%d",
            trytime,
            artifact_relpath(out_c_file, logpath),
            loops.invariant_count(),
        )

        output_result_type, out_std, out_err, _solve_time = \
            framac.run_framac_with_wp(logpath, os.path.basename(out_c_file), 10)
        logger.info(
            "[FRAMAC] attempt=%d result=%s stdout=%s stderr=%s",
            trytime,
            output_result_type,
            artifact_relpath(out_std, logpath),
            artifact_relpath(out_err, logpath),
        )

        with open(out_std, 'r') as f:
            log = f.read()

        # ---------- Case: Invalid ----------
        if output_result_type == "Invalid":
            invalid_msg = framac.parse_frama_invalid_output(code_with_inv, log)
            logger.info(
                "[INVALID] attempt=%d remove=%s",
                trytime,
                ", ".join(invalid_msg.keys()) if invalid_msg else "none",
            )
            if not invalid_msg:
                logger.info("[INVALID] attempt=%d no removable invariant found; stop this run", trytime)
                return "", proposal, False
            for k in invalid_msg:
                loops.remove_invariant_by_id(k)
            trytime += 1

        # ---------- Case: Fail ----------
        elif output_result_type.startswith("Fail_"):
            error_msg = framac.parse_frama_valid_output(log)
            logger.info(
                "[FAIL] attempt=%d failures=%d summary=%s",
                trytime,
                len(error_msg),
                failure_summary(error_msg),
            )

            terminate = True
            for k, v in error_msg.items():
                if k.startswith("i"):
                    terminate = False

                loops.update_error_count(k)
                loops.remove_invariant_by_id(k)

            if terminate:
                return "", proposal, False

            trytime += 1

        # ---------- Case: Pass ----------
        elif output_result_type.startswith("Pass_"):
            logger.info("[PASS] attempt=%d candidate=%s", trytime, artifact_relpath(out_c_file, logpath))
            return code_with_inv, proposal, True

        else:
            logger.error(f"Unknown Frama-C result: {output_result_type}")
            return "", proposal, False

    return "", proposal, False


def verify_batch_feedback_ablation(code, file_basename, logpath, logger,
                                   local_model_context=None, source_extension=".c"):
    trytime = 1
    proposal = 0
    max_rounds = 5

    loops = processor.build_loops(
        code,
        file_basename,
        model,
        logger,
        local_model_context=local_model_context,
    )
    logger.info(
        "[ABLATION-BATCH] initial_prompt=full feedback=all_errors_single_prompt keep_existing_invariants=true max_rounds=%d",
        max_rounds,
    )
    logger.info(
        "[INIT] functions=%s loops=%d",
        ", ".join(region["name"] for region in loops.function_regions),
        len(loops.loop_list),
    )
    loops.initial_loop_invariant_inference()

    while trytime <= max_rounds:
        loops.logger_inv()
        code_with_inv = loops.inject_invariants()

        out_c_file = os.path.join(
            logpath,
            f"{file_basename}_verified_batch_feedback_{trytime}{source_extension}",
        )
        with open(out_c_file, "w", encoding="utf-8") as f:
            f.write(code_with_inv)

        proposal += 1
        logger.info(
            "[ATTEMPT %d] phase=batch_feedback_ablation candidate=%s invariants=%d",
            trytime,
            artifact_relpath(out_c_file, logpath),
            loops.invariant_count(),
        )

        output_result_type, out_std, out_err, _solve_time = framac.run_framac_with_wp(
            logpath,
            os.path.basename(out_c_file),
        )
        logger.info(
            "[FRAMAC] attempt=%d result=%s stdout=%s stderr=%s",
            trytime,
            output_result_type,
            artifact_relpath(out_std, logpath),
            artifact_relpath(out_err, logpath),
        )

        with open(out_std, "r") as f:
            log = f.read()

        if output_result_type.startswith("Pass_"):
            logger.info("[PASS] attempt=%d candidate=%s", trytime, artifact_relpath(out_c_file, logpath))
            return code_with_inv, proposal, True

        if output_result_type == "Invalid":
            invalid_msg = framac.parse_frama_invalid_output(code_with_inv, log)
            logger.info(
                "[INVALID] attempt=%d cleanup_remove=%s",
                trytime,
                ", ".join(invalid_msg.keys()) if invalid_msg else "none",
            )
            if not invalid_msg:
                return "", proposal, False
            for invariant_id in invalid_msg:
                loops.remove_invariant_by_id(invariant_id)
            trytime += 1
            continue

        if output_result_type.startswith("Fail_"):
            error_msg = framac.parse_frama_valid_output(log)
            logger.info(
                "[FAIL] attempt=%d failures=%d summary=%s",
                trytime,
                len(error_msg),
                failure_summary(error_msg),
            )
            if not error_msg:
                return "", proposal, False
            loops.batch_feedback_refine_failure(error_msg)
            trytime += 1
            continue

        logger.error("[ABLATION-BATCH] unsupported Frama-C result: %s", output_result_type)
        return "", proposal, False

    logger.info("[ABLATION-BATCH] Verification failed: reached %d total proposal rounds.", max_rounds)
    return "", proposal, False


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default='_binary_search.cbs', type=str)
    parser.add_argument("--output", default='../Result/', type=str)
    parser.add_argument("--model", default="deepseek-flash", type=str)
    parser.add_argument("--proposal", default=5, type=int,
                        help="Bounded repair attempts before unlimited final repair (default: 5).")
    parser.add_argument("--dataset", default='acsl-algorithms', type=str)
    parser.add_argument("--sample-timeout", default=0, type=int,
                        help="Wall-clock timeout in seconds for each sample; 0 disables timeout.")
    parser.add_argument("--local-model", action="store_true",
                        help="Use a Hugging Face compatible local model instead of an API model.")
    parser.add_argument("--local-model-path", default="", type=str,
                        help="Path to the local model directory. Defaults to --model when --local-model is set.")
    parser.add_argument("--local-device-map", default="auto", type=str,
                        help="Device map passed to AutoModelForCausalLM.from_pretrained.")
    parser.add_argument("--local-torch-dtype", default="auto", type=str,
                        help="Torch dtype passed to AutoModelForCausalLM.from_pretrained.")
    parser.add_argument("--local-max-new-tokens", default=32768, type=int,
                        help="Maximum new tokens for local model generation.")
    parser.add_argument("--local-eos-token-id", default=None, type=int,
                        help="Optional EOS token id for local model generation.")
    parser.add_argument(
        "--ablation-mode",
        default="none",
        choices=["none", "plain", "batch-feedback"],
        help=(
            "Run an ablation path. "
            "'plain' uses plain-code initial prompts and feedback prompts without existing loop invariants. "
            "'batch-feedback' keeps full initial inference but replaces diagnosis/refinement with one prompt containing all Frama-C errors."
        ),
    )
    parser.add_argument("--runAll", action="store_true")

    args = parser.parse_args()

    if args.ablation_mode == "plain" and args.output == '../Result/':
        args.output = '../ResultAblationPlain/'
    if args.ablation_mode == "batch-feedback" and args.output == '../Result/':
        args.output = '../ResultAblationBatchFeedback/'
        
    maxtime = args.proposal
    model = args.model

    local_model_context = None
    if args.local_model:
        from transformers import AutoModelForCausalLM, AutoTokenizer

        local_model_path = args.local_model_path or args.model
        tokenizer = AutoTokenizer.from_pretrained(
            local_model_path,
            use_fast=False,
            trust_remote_code=True,
            local_files_only=True,
        )
        local_model = AutoModelForCausalLM.from_pretrained(
            local_model_path,
            trust_remote_code=True,
            torch_dtype=args.local_torch_dtype,
            device_map=args.local_device_map,
            local_files_only=True,
        )
        local_model_context = {
            "path": local_model_path,
            "tokenizer": tokenizer,
            "model": local_model,
            "max_new_tokens": args.local_max_new_tokens,
            "eos_token_id": args.local_eos_token_id,
        }
    # ------------------------------------------------
    # -------- Helper: prepare logger per file -------
    # ------------------------------------------------
    def build_logger(logfile: str):
        """
        Build an isolated logger for each file.
        Ensures no handler contamination from previous files.
        """
        logger = logging.getLogger('loopinvinfer')

        # Clear old handlers
        if logger.hasHandlers():
            for h in logger.handlers[:]:
                logger.removeHandler(h)
                h.close()

        logger.setLevel(logging.INFO)
        logger.propagate = False

        # Init log file
        with open(logfile, 'w', encoding="utf-8") as f:
            f.truncate()

        # Add new handlers
        formatter = logging.Formatter("%(levelname)s | %(message)s")
        fh = logging.FileHandler(logfile, encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh.setFormatter(formatter)
        sh = logging.StreamHandler()
        sh.setLevel(logging.INFO)
        sh.setFormatter(formatter)

        logger.addHandler(fh)
        logger.addHandler(sh)
        logger.artifact_dir = os.path.dirname(logfile)

        return logger

    # ------------------------------------------------
    # -------- Helper: process one file --------------
    # ------------------------------------------------
    def process_file(fname: str):
        """
        Process one .c or .cbs file: build logger, read code, verify, save results.
        """

        codepath = '../Benchmark/' + args.dataset +'/' + fname
        log_dir = os.path.join(args.output, args.dataset, model, fname)

        # Rebuild directory
        if os.path.exists(log_dir):
            return
        os.makedirs(log_dir)

        logfile = os.path.join(log_dir, "log.log")
        logger = build_logger(logfile)
        
        if local_model_context:
            logger.info(
                "[MODEL] type=local path=%s device=%s",
                local_model_context["path"],
                getattr(local_model_context["model"], "device", "unknown"),
            )
        else:
            logger.info("[MODEL] type=%s", model)
        logger.info("[INPUT] source=%s", codepath)
        with open(codepath, 'r') as f:
            code = f.read()
        source_extension = os.path.splitext(fname)[1].lower() or ".c"
        input_artifact = os.path.join(log_dir, "input" + source_extension)
        with open(input_artifact, "w", encoding="utf-8") as f:
            f.write(code)
        logger.info("[INPUT] artifact=%s bytes=%d", artifact_relpath(input_artifact, log_dir), len(code))

        t_start = time.time()
        verified_code = ""
        number_attempts = 0
        result = False
        timed_out = False
        old_alarm_handler = None
        try:
            if args.sample_timeout and args.sample_timeout > 0:
                old_alarm_handler = signal.signal(signal.SIGALRM, sample_timeout_handler)
                signal.alarm(args.sample_timeout)
                logger.info("[TIMEOUT] sample_timeout=%d seconds", args.sample_timeout)
            if args.ablation_mode == "batch-feedback":
                verified_code, number_attempts, result = verify_batch_feedback_ablation(
                    code=code,
                    file_basename=fname.split('.')[0],
                    logpath=log_dir,
                    logger=logger,
                    local_model_context=local_model_context,
                    source_extension=source_extension,
                )
            else:
                verified_code, number_attempts, result = verify(
                    code=code,
                    file_basename=fname.split('.')[0],
                    logpath=log_dir,
                    logger=logger,
                    maxtime=maxtime,
                    local_model_context=local_model_context,
                    ablation_mode=args.ablation_mode == "plain",
                    source_extension=source_extension,
                )
        except SampleTimeout:
            timed_out = True
            logger.error("[TIMEOUT] sample exceeded %d seconds; marking Fail", args.sample_timeout)
        finally:
            if args.sample_timeout and args.sample_timeout > 0:
                signal.alarm(0)
                if old_alarm_handler is not None:
                    signal.signal(signal.SIGALRM, old_alarm_handler)
        t_finish = time.time()

        # Store all results
        verified_artifact = ""
        if result and verified_code:
            verified_artifact = os.path.join(log_dir, "verified" + source_extension)
            with open(verified_artifact, "w", encoding="utf-8") as f:
                f.write(verified_code)

        logger.info("---------Result---------")
        logger.info("Success" if result else "Fail")
        logger.info(f"Model: {model}")
        logger.info(f"Proposal number: {number_attempts}")
        logger.info("Verified code: %s", artifact_relpath(verified_artifact, log_dir) if verified_artifact else "")
        if timed_out:
            logger.info("Timeout: %d", args.sample_timeout)
        logger.info(f"Running time: {t_finish - t_start}")
        logger.info("------------------------")

    def log_dir_for_file(fname: str):
        return os.path.join(args.output, args.dataset, model, fname)

    def log_has_result(logfile: str) -> bool:
        if not os.path.exists(logfile):
            return False
        with open(logfile, "r", encoding="utf-8", errors="ignore") as f:
            return "---------Result---------" in f.read()

    def count_logged_attempts(logfile: str) -> int:
        if not os.path.exists(logfile):
            return 0
        with open(logfile, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
        return (
            text.count("phase=bounded candidate=")
            + text.count("phase=final candidate=")
            + text.count("phase=batch_feedback_ablation candidate=")
        )

    def append_forced_result(fname: str, reason: str, elapsed: float):
        log_dir = log_dir_for_file(fname)
        os.makedirs(log_dir, exist_ok=True)
        logfile = os.path.join(log_dir, "log.log")
        if log_has_result(logfile):
            return
        attempts = count_logged_attempts(logfile)
        with open(logfile, "a", encoding="utf-8") as f:
            f.write(f"ERROR | [{reason}] sample stopped; marking Fail\n")
            f.write("INFO | ---------Result---------\n")
            f.write("INFO | Fail\n")
            f.write(f"INFO | Model: {model}\n")
            f.write(f"INFO | Proposal number: {attempts}\n")
            f.write("INFO | Verified code: \n")
            if reason == "TIMEOUT":
                f.write(f"INFO | Timeout: {args.sample_timeout}\n")
            f.write(f"INFO | Running time: {elapsed}\n")
            f.write("INFO | ------------------------\n")

    def process_file_child(fname: str):
        os.setsid()
        process_file(fname)

    def process_file_with_timeout(fname: str):
        if not args.sample_timeout or args.sample_timeout <= 0:
            process_file(fname)
            return
        if os.path.exists(log_dir_for_file(fname)):
            return

        ctx = multiprocessing.get_context("fork")
        proc = ctx.Process(target=process_file_child, args=(fname,))
        t_start = time.time()
        proc.start()
        proc.join(args.sample_timeout)
        elapsed = time.time() - t_start

        if proc.is_alive():
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            proc.join(5)
            if proc.is_alive():
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.join()
            append_forced_result(fname, "TIMEOUT", min(elapsed, float(args.sample_timeout)))
            print(f"[TIMEOUT] {args.dataset}/{fname} exceeded {args.sample_timeout}s; continue.")
            return

        if proc.exitcode not in (0, None):
            append_forced_result(fname, "CRASH", elapsed)
            print(f"[CRASH] {args.dataset}/{fname} exitcode={proc.exitcode}; continue.")

    # ------------------------------------------------
    # ------ RUN ALL FILES IN DIRECTORY --------------
    # ------------------------------------------------
    if args.runAll:
        DATASETS = [
            dataset
            for dataset in ('OOPSLA', 'SVCOMP', 'acsl-algorithms')
            if os.path.isdir(f'../Benchmark/{dataset}')
        ]
        for dataset in DATASETS:
            args.dataset = dataset
            base = f'../Benchmark/{args.dataset}/'
            for fname in os.listdir(base):
                supported_extensions = (".cbs",) if dataset == "acsl-algorithms" else (".c",)
                if fname.lower().endswith(supported_extensions):
                    process_file_with_timeout(fname)
            print("\n>>> All algorithms processed.")
            
        sys.exit(0)
    # ------------------------------------------------
    # -------------- RUN SINGLE FILE -----------------
    # ------------------------------------------------
    process_file_with_timeout(args.file)
