import os, io, sys, time, re
import logging
import subprocess
import tempfile
import datetime
from typing import List

Check_STDOUT = 1                    # Set 1 if you want to record std result
Check_STDERR = 1                    # Set 1 if you want to record err result
SubprocessTimeout = 60              # Set the timeout of subprocess
SPECIAL_WP_BENCHMARKS = ("_dijkstra", "_dijstra", "_bellmanford")


# create subprocess according to the value of Check_STDOUT and Check_STDERR
def create_FRAMAC_subprocess(FRAMAC_Command, Check_STDOUT, Check_STDERR):
    # Create the FRAMAC command
    # Check_STDOUT and Check_STDERR are used to check the standard output and error of the FRAMAC subprocess
    if (Check_STDOUT == 1 and Check_STDERR == 1):
        process = subprocess.Popen(FRAMAC_Command, close_fds=True, preexec_fn=os.setpgrp, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    elif (Check_STDOUT == 1 and Check_STDERR == 0):
        process = subprocess.Popen(FRAMAC_Command, close_fds=True, preexec_fn=os.setpgrp, stdout=subprocess.PIPE)
    elif (Check_STDOUT == 0 and Check_STDERR == 1):
        process = subprocess.Popen(FRAMAC_Command, close_fds=True, preexec_fn=os.setpgrp, stderr=subprocess.PIPE)
    else:
        process = subprocess.Popen(FRAMAC_Command, close_fds=True, preexec_fn=os.setpgrp)
    return process


def get_result_type(context_bytes):
    # Convert from bytes to a list of strings (split by newline characters).
    result_type = "UK"
    try:
        context_strings = context_bytes.decode("utf-8")
        context_io = io.StringIO(context_strings)
        context = context_io.readlines()
    except:
        return result_type
    
    # Iterate through each line in the context.
    timeout_in_requires = 0
    for line in context:
        # If the line contains the string "[kernel] Frama-C aborted:", then the
        # build is invalid.
        if "[kernel] Frama-C aborted:" in line or "[kernel] Plug-in wp aborted" in line or "[wp] Warning: No goal generated" in line or "error: invalid preprocessing directive" in line:
            result_type = "Invalid"
            break
        elif "[wp] [Timeout] typed_" in line and ("_requires (" in line or "_requires_" in line):
            timeout_in_requires += 1
            continue
        # If the line contains the string "[wp] Proved goals:", then the build
        # is valid. The number of proved goals is given in the form "x/y",
        # where x is the number of proved goals and y is the number of total
        # goals. If x == y, then the build is a pass. Otherwise, the build is
        # a fail.
        elif "[wp] Proved goals:" in line:
            proportion = line.split(":")[-1]
            left, right = proportion.split("/")
            left = left.strip()
            right = right.strip()
            if int(left) + int(timeout_in_requires) == int(right):
                result_type = "Pass_" + left + "_" + right
            else:
                result_type = "Fail_" + left + "_" + right
            break

    return result_type

def uses_special_wp_profile(gfile: str) -> bool:
    base = os.path.basename(gfile).lower()
    return any(name in base for name in SPECIAL_WP_BENCHMARKS)


def build_framac_compatibility_source(source: bytes) -> bytes:
    """Return a same-size C/ACSL view of native TrustC source."""
    source = re.sub(
        rb"\b_(?:Safe|Unsafe|Borrow|Owned|Nonnull|Nullable)\b",
        lambda match: b" " * len(match.group(0)),
        source,
    )
    # Keep the standard C address-of operator and mask only TrustC's explicit
    # borrow-kind suffix.  Replacing nullptr with a padded integer null pointer
    # constant also preserves byte and line offsets in diagnostics.
    source = re.sub(
        rb"\b_(?:Mut|Const)\b",
        lambda match: b" " * len(match.group(0)),
        source,
    )
    source = re.sub(rb"\bnullptr\b", b"0      ", source)
    return source


def build_framac_wp_command(Output_folder, gfile, time_out=10, target_file=None):
    target_file = target_file or os.path.join(Output_folder, gfile)
    if uses_special_wp_profile(gfile):
        return [
            "frama-c",
            "-wp",
            "-wp-rte",
            "-wp-prop=-@lemma",
            "-wp-print",
            "-wp-prover",
            "alt-ergo,z3,cvc5,coq",
            "-wp-cache",
            "none",
            "-wp-script",
            "init",
            "-wp-interactive",
            "batch",
            "-wp-interactive-timeout",
            "120",
            "-wp-timeout",
            "60",
            "-wp-memlimit",
            "65536",
            target_file,
        ]
    return [
        "frama-c",
        "-wp",
        "-wp-prop=-@terminates, -@lemma",
        "-wp-print",
        "-wp-prover",
        "alt-ergo,z3,cvc5",
        "-wp-timeout",
        str(time_out),
        target_file,
    ]


def run_framac_with_wp(Output_folder, gfile, time_out = 10):
    starttime = datetime.datetime.now()
    source_file = os.path.join(Output_folder, gfile)
    compatibility_file = ""
    target_file = source_file
    if os.path.splitext(gfile)[1].lower() == ".cbs":
        # Frama-C delegates preprocessing to GCC, which does not recognize the
        # TrustC extension.  TrustC's ACSL-style verification subset is C/ACSL
        # compatible, so verify an ephemeral .c view while keeping all user
        # artifacts in their original .cbs form.
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".col2inv-trustc-",
            suffix=".c",
            dir=Output_folder,
            delete=False,
        ) as temporary:
            compatibility_file = temporary.name
            with open(source_file, "rb") as source:
                compatibility_source = source.read()
            # Frama-C verifies the ACSL/C view, while TrustC checks the native
            # source separately. Mask TrustC-only syntax without changing byte
            # offsets or line numbers in diagnostics.
            compatibility_source = build_framac_compatibility_source(
                compatibility_source
            )
            temporary.write(compatibility_source)
        target_file = compatibility_file

    FRAMAC_Command = build_framac_wp_command(
        Output_folder,
        gfile,
        time_out,
        target_file=target_file,
    )

    def cleanup_compatibility_file():
        if not compatibility_file:
            return
        try:
            os.unlink(compatibility_file)
        except FileNotFoundError:
            pass
    
    # create subprocess according to the value of check_STDOUT and check_STDERR
    try:
        process = create_FRAMAC_subprocess(FRAMAC_Command, Check_STDOUT, Check_STDERR)
    except Exception:
        cleanup_compatibility_file()
        raise
    logging.info("[CMD] Running `" + ' '.join(FRAMAC_Command) + "`")

    output_std_file_name = ""
    output_err_file_name = ""
    output_result_type = ""
    try:
        # join subprocess
        if Check_STDOUT == 1 or Check_STDERR == 1:
            stdoutdata, stderrdata = process.communicate(timeout=SubprocessTimeout)
            fleft, _ = os.path.splitext(gfile)
            if stdoutdata != b'':
                result_type = get_result_type(stdoutdata)
                output_result_type = result_type
                fleft = fleft.replace("_gen_", "_fstd_")
                fstd_file_name = fleft + "_" + result_type
                output_std_file_name = os.path.join(Output_folder, fstd_file_name + ".txt")
                with open (output_std_file_name, "wb") as stdfile:
                    #stdfile.write(bytes(str(pattern)+"\n", encoding = "utf8"))
                    stdfile.write(stdoutdata)
            if stderrdata != b'':
                fleft, _ = os.path.splitext(gfile)
                fleft = fleft.replace("_gen_", "_ferr_")
                output_err_file_name = os.path.join(Output_folder, fleft + ".txt")
                with open (output_err_file_name, "wb") as errfile:
                    errfile.write(stderrdata)
        else:
            process.communicate(timeout=SubprocessTimeout)
    
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, 15)
            time.sleep(1)
            if process.poll() is None:
                os.killpg(process.pid, 9)
        except ProcessLookupError:
            pass
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

        try:
            stdoutdata, stderrdata = process.communicate(timeout=2)
        except Exception:
            stdoutdata = exc.stdout or b""
            stderrdata = exc.stderr or b""
        if isinstance(stdoutdata, str):
            stdoutdata = stdoutdata.encode("utf-8", errors="replace")
        if isinstance(stderrdata, str):
            stderrdata = stderrdata.encode("utf-8", errors="replace")

        fleft, _ = os.path.splitext(gfile)
        output_result_type = "Timeout"
        output_std_file_name = os.path.join(Output_folder, fleft + "_Timeout.txt")
        with open(output_std_file_name, "wb") as stdfile:
            stdfile.write(stdoutdata or b"")
            if stderrdata:
                stdfile.write(b"\n[stderr]\n")
                stdfile.write(stderrdata)
        logging.error("Timeout for subprocess when running frama-c on " + os.path.join(Output_folder, gfile))
    
    except Exception as e:
        print("\033[34m\tUnknown Exception" + "\033[0m")
        print(e)
        cleanup_compatibility_file()
        raise e
    endtime = datetime.datetime.now()
    solve_time = endtime - starttime
    cleanup_compatibility_file()
    return output_result_type, output_std_file_name, output_err_file_name, solve_time

import re

import re

def extract_wp_goal_context(block: str) -> str:
    lines = block.splitlines()
    start = None
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("Let ") or stripped.startswith("Assume {") or stripped.startswith("Prove:"):
            start = idx
            break
    if start is None:
        return ""

    context_lines = []
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.startswith("Prover "):
            break
        context_lines.append(line.rstrip())
    return "\n".join(context_lines).strip()


def parse_frama_valid_output(log_text: str) -> dict:
    result = {}

    # ==========================================
    # 第一步：扫描概要部分，确定哪些 ID 失败了
    # ==========================================
    
    # 匹配 Loop Invariant 的 Establishment 失败
    pattern_est_fail = re.compile(
        r"\[wp\]\s*\[(?:Timeout|Unknown)\]\s*typed_[\w]+_loop_invariant_(i_\d+)_established", re.I
    )
    # 匹配 Loop Invariant 的 Preservation 失败
    pattern_pre_fail = re.compile(
        r"\[wp\]\s*\[(?:Timeout|Unknown)\]\s*typed_[\w]+_loop_invariant_(i_\d+)_preserved", re.I
    )
    # 匹配 Ensures (Post-condition) 失败
    pattern_ens_fail = re.compile(
        r"\[wp\]\s*\[(?:Timeout|Unknown)\]\s*typed_(?P<func>[\w]+)_ensures_(?P<goal>e_\d+)", re.I
    )
    # 匹配 Assert 失败
    pattern_assert_fail = re.compile(
        r"\[wp\]\s*\[(?:Timeout|Unknown)\]\s*typed_(?P<func>[\w]+)_assert_(?P<goal>a_\d+)", re.I
    )

    # 记录需要查找上下文的目标集合
    # 格式: { 'i_2': {'preservation', 'establishment'}, 'e_1': {'post-condition'} }
    targets_to_find = {}

    def add_target(g_id, g_type):
        if g_id not in result:
            result[g_id] = {"status": None, "property": [], "context": []}
        
        if g_id not in targets_to_find:
            targets_to_find[g_id] = set()
        targets_to_find[g_id].add(g_type)
        if g_type not in result[g_id]["property"]:
            result[g_id]["property"].append(g_type)

    # 1. 扫描 Establishment 失败
    for m in pattern_est_fail.finditer(log_text):
        g_id = m.group(1)
        add_target(g_id, "establishment")
        result[g_id]["status"] = "establishment"

    # 2. 扫描 Preservation 失败
    for m in pattern_pre_fail.finditer(log_text):
        g_id = m.group(1)
        add_target(g_id, "preservation")
        if result[g_id]["status"] == "establishment":
            result[g_id]["status"] = "both"
        else:
            result[g_id]["status"] = "preservation"

    # 3. 扫描 Ensures 失败
    for m in pattern_ens_fail.finditer(log_text):
        g_id = f"{m.group('func')}:{m.group('goal')}"
        add_target(g_id, "post-condition")
        result[g_id]["status"] = False

    # 4. 扫描 Assert 失败
    for m in pattern_assert_fail.finditer(log_text):
        g_id = f"{m.group('func')}:{m.group('goal')}"
        add_target(g_id, "assertion")
        result[g_id]["status"] = False

    # ==========================================
    # 第二步：扫描详细部分，提取完整 WP Goal（Let + Assume + Prove）
    # ==========================================

    # 使用分割线将文件拆分为一个个 Goal 块
    # 匹配至少 20 个连续的横线（根据你的示例是 60 个，这里放宽一点以防万一）
    blocks = re.split(r"^-{20,}$", log_text, flags=re.MULTILINE)

    # 定义详细块的标题匹配正则
    # 匹配: Goal Preservation of Invariant 'i_2'
    header_preservation = re.compile(r"Goal Preservation of Invariant '(i_\d+)'", re.I)
    # 匹配: Goal Establishment of Invariant 'i_2'
    header_establishment = re.compile(r"Goal Establishment of Invariant '(i_\d+)'", re.I)
    # 匹配: Goal Post-condition 'e_1'
    header_post = re.compile(
        r"Goal Post-condition(?: for '[^']+')? '(e_\d+)'(?: in '([\w]+)')?",
        re.I,
    )
    # 匹配: Goal Assertion 'a_1'
    header_assert = re.compile(r"Goal Assertion '(a_\d+)'(?: in '([\w]+)')?", re.I)

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        # 判断当前块属于哪种类型，针对哪个 ID
        current_id = None
        current_type = None

        if m := header_preservation.search(block):
            current_id = m.group(1)
            current_type = "preservation"
        elif m := header_establishment.search(block):
            current_id = m.group(1)
            current_type = "establishment"
        elif m := header_post.search(block):
            current_id = f"{m.group(2)}:{m.group(1)}" if m.group(2) else m.group(1)
            current_type = "post-condition"
        elif m := header_assert.search(block):
            current_id = f"{m.group(2)}:{m.group(1)}" if m.group(2) else m.group(1)
            current_type = "assertion"

        # 如果当前块是我们之前标记为失败的目标
        if current_id and current_id in targets_to_find:
            if current_type in targets_to_find[current_id]:
                context = extract_wp_goal_context(block)
                if context:
                    if result[current_id]["status"] == "both":
                        result[current_id]["context"].append(f"--- {current_type.capitalize()} Context ---")
                    result[current_id]["context"].append(context)

    # ==========================================
    # 第三步：扁平化 context 列表
    # ==========================================
    for g_id in result:
        # 将列表中的字符串用换行符连接
        if isinstance(result[g_id].get("property"), list):
            result[g_id]["property"] = ", ".join(result[g_id]["property"])
        result[g_id]["context"] = "\n".join(result[g_id]["context"])

    return result


def parse_frama_invalid_output(c_code: str, log_text: str):
    to_delete = {}

    if "Frama-C aborted: invalid user input" not in log_text:
        return to_delete

    annot_pattern = re.compile(
        r":\s*(\d+)\s*:\s*Warning:[\s\S]{0,400}?Ignoring loop annotation",
        flags=re.IGNORECASE
    )
    error_line_strings = annot_pattern.findall(log_text)
    error_lines = sorted({int(x) for x in error_line_strings})  

    if not error_lines:
        wide_pat = re.compile(r"\[kernel:annot-error\][\s\S]{0,500}?:\s*(\d+)\s*:\s*", re.I)
        found = wide_pat.findall(log_text)
        if found:
            error_lines = sorted({int(x) for x in found})

    if not error_lines:
        return to_delete

    inv_pat = re.compile(
        r"(loop\s+invariant\s+(?P<id>i_\d+)\s*:\s*(?P<body>.*?);)",
        flags=re.IGNORECASE | re.S
    )

    invariants = [] 
    for m in inv_pat.finditer(c_code):
        full_text = m.group(1).strip()
        inv_id = m.group("id")
        start_idx = m.start(1)
        end_idx = m.end(1)
        start_line = c_code[:start_idx].count("\n") + 1
        end_line = c_code[:end_idx].count("\n") + 1
        invariants.append((inv_id, full_text, start_line, end_line))

    for err_line in error_lines:
        for inv_id, full_text, sline, eline in invariants:
            if sline <= err_line <= eline:
                to_delete[inv_id] = full_text

    return to_delete


if __name__ == "__main__":
    with open('./output.txt', 'r') as f: 
        log = f.read()
    # print(log)
    res = parse_frama_valid_output(log)
    # print(res)
    # print(c_code)
    # print(log_text)
    # del_map = parse_frama_invalid_output(c_code, log_text)
    # print(del_map)
    for k,v in res.items():
        print(k, v)
