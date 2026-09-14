from loop import Loop
import openai
import re, prompt, string
import requests
import json
import os
import logging
from typing import Tuple, Optional

def scan_clause_from(text: str, start_pos: int) -> Tuple[Optional[str], int]:
    i = start_pos
    n = len(text)
    buf = []
    paren = 0   # ( )
    brack = 0   # [ ]
    brace = 0   # { }
    skip_header_semicolon = 0  # 若>0表示最近遇到 \forall 或 \exists，需要跳过接下来的一个分号（头部分号）
    # 为了正确识别量词，需检测反斜杠后面是 forall 或 exists
    def starts_new_invariant(pos: int) -> bool:
        return re.match(r"\s*loop\s+invariant\b", text[pos:], re.I) is not None

    while i < n:
        ch = text[i]
        if buf and starts_new_invariant(i):
            clause = ''.join(buf).strip()
            if clause:
                return clause, i
            return None, i

        # 检测量词启动：\forall 或 \exists（允许前后空白）
        if ch == '\\':
            # check following word
            m = re.match(r"\\(forall|exists)\b", text[i:])
            if m:
                # copy the matched text into buffer, advance i
                token = m.group(0)
                buf.append(token)
                i += len(token)
                # after a quantifier, the header will contain a semicolon that we must skip once
                # (there may be multiple quantifiers, so increment skip count)
                skip_header_semicolon += 1
                continue

        if paren == 0 and brack == 0 and brace == 0:
            if text.startswith("```", i) or re.match(r"\[Loop\b", text[i:]):
                clause = ''.join(buf).strip()
                if clause:
                    return clause, i
                return None, i

        # track brackets/parens
        if ch == '(':
            paren += 1
            buf.append(ch); i += 1; continue
        if ch == ')':
            if paren > 0: paren -= 1
            buf.append(ch); i += 1; continue
        if ch == '[':
            brack += 1
            buf.append(ch); i += 1; continue
        if ch == ']':
            if brack > 0: brack -= 1
            buf.append(ch); i += 1; continue
        if ch == '{':
            brace += 1
            buf.append(ch); i += 1; continue
        if ch == '}':
            if brace > 0: brace -= 1
            buf.append(ch); i += 1; continue

        # semicolon handling
        if ch == ';':
            if starts_new_invariant(i + 1):
                clause = ''.join(buf).strip()
                return clause, i + 1
            # if inside any brackets/parens/braces, this semicolon likely part of an expression -> keep scanning
            if paren > 0 or brack > 0 or brace > 0:
                buf.append(ch); i += 1; continue
            # if we have pending quantifier header semicolons to skip, skip one
            if skip_header_semicolon > 0:
                # consume this semicolon as header separator, decrement counter and continue
                skip_header_semicolon -= 1
                buf.append(ch)  # we may keep it inside buffer (or omit). We keep it since body may rely on it.
                i += 1
                continue
            # Otherwise this semicolon ends the clause
            clause = ''.join(buf).strip()
            return clause, i + 1
        # normal char
        buf.append(ch)
        i += 1

    # reached EOF without semicolon terminator
    clause = ''.join(buf).strip()
    if clause:
        return clause, n
    return None, n


CONTROL_FLOW_KEYWORDS = {
    "if", "for", "while", "switch", "return", "sizeof", "do",
}


def get_api_client_config(model: str):
    model_lower = model.lower()
    if "deepseek" in model_lower:
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError(
                "DEEPSEEK_API_KEY is not set. "
                "Please export it before running loopinvinfer.py."
            )
        api_model = model.split("/", 1)[1] if model_lower.startswith("deepseek/") else model
        return "deepseek", "https://api.deepseek.com", api_key, api_model

    if "qwen" in model_lower:
        api_key = os.environ.get("QWEN_API_KEY")
        if not api_key:
            raise RuntimeError(
                "QWEN_API_KEY is not set. "
                "Please export it before running loopinvinfer.py."
            )
        return (
            "qwen",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key,
            model,
        )

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. "
            "Please export it before running loopinvinfer.py."
        )
    return "openrouter", "https://openrouter.ai/api/v1", api_key, model


def mask_comments_and_strings(code: str) -> str:
    """Return code with comments and strings blanked while preserving offsets."""
    chars = list(code)
    i = 0
    n = len(chars)

    def blank(pos: int):
        if chars[pos] != "\n":
            chars[pos] = " "

    while i < n:
        if code.startswith("/*", i):
            blank(i)
            blank(i + 1)
            i += 2
            while i < n:
                if code.startswith("*/", i):
                    blank(i)
                    if i + 1 < n:
                        blank(i + 1)
                    i += 2
                    break
                blank(i)
                i += 1
            continue

        if code.startswith("//", i):
            blank(i)
            blank(i + 1)
            i += 2
            while i < n and code[i] != "\n":
                blank(i)
                i += 1
            continue

        if code[i] in ("'", '"'):
            quote = code[i]
            blank(i)
            i += 1
            escaped = False
            while i < n:
                ch = code[i]
                blank(i)
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == quote:
                    i += 1
                    break
                i += 1
            continue

        i += 1

    return "".join(chars)


def find_matching_brace(masked_code: str, open_pos: int) -> Optional[int]:
    depth = 0
    for i in range(open_pos, len(masked_code)):
        ch = masked_code[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def find_matching_paren(masked_code: str, open_pos: int) -> Optional[int]:
    depth = 0
    for i in range(open_pos, len(masked_code)):
        ch = masked_code[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def split_top_level_commas(text: str):
    parts = []
    start = 0
    paren = 0
    brack = 0
    brace = 0
    for i, ch in enumerate(text):
        if ch == "(":
            paren += 1
        elif ch == ")":
            paren = max(paren - 1, 0)
        elif ch == "[":
            brack += 1
        elif ch == "]":
            brack = max(brack - 1, 0)
        elif ch == "{":
            brace += 1
        elif ch == "}":
            brace = max(brace - 1, 0)
        elif ch == "," and paren == 0 and brack == 0 and brace == 0:
            parts.append(text[start:i].strip())
            start = i + 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def find_function_regions(code: str):
    masked = mask_comments_and_strings(code)
    func_re = re.compile(
        r"\b(?P<name>[A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{",
        re.S,
    )

    regions = []
    search_pos = 0
    while True:
        m = func_re.search(masked, search_pos)
        if not m:
            break

        name = m.group("name")
        open_pos = m.end() - 1
        end_pos = find_matching_brace(masked, open_pos)
        if end_pos is None:
            search_pos = m.end()
            continue

        if name not in CONTROL_FLOW_KEYWORDS:
            regions.append({
                "name": name,
                "name_start": m.start("name"),
                "body_start": open_pos + 1,
                "body_end": end_pos,
            })

        search_pos = end_pos + 1

    return regions


def loop_label(index: int) -> str:
    return string.ascii_uppercase[index] if index < len(string.ascii_uppercase) else f"Z{index - 25}"

class LoopList:
    def __init__(self, code, file_basename, model, logger, function_regions=None, local_model_context=None, ablation_mode=False) -> None:
        self.loop_list: list[Loop] = []
        self.file_basename = file_basename
        self.filename = file_basename
        self.raw_code = code
        self.function_regions = function_regions or find_function_regions(code)
        self.function_region_by_name = {region["name"]: region for region in self.function_regions}
        self.invid = 0
        self.model = model
        self.local_model_context = local_model_context
        self.ablation_mode = ablation_mode
        self.logger: logging.Logger = logger
        self.requires_by_function = self.extract_file_contracts(code, "requires")
        self.asserts_by_function = self.extract_file_asserts(code)
        self.ensures_by_function = self.extract_file_contracts(code, "ensures", include_asserts=True)
        self.parameters_by_function = self.extract_function_parameters(code)
        self.ensures: dict = self.flatten_function_clauses(self.ensures_by_function)
        self.code = self.inject_loop_index_into_code(code)
        self.error_count = {}
        self.parent_by_loop = {}
        self.children_by_loop = {}
        self.transition_edges = []
        self.call_edges = []
        self.llm_call_count = 0
        self.invariant_snapshot_count = 0

    def artifact_root(self):
        return getattr(self.logger, "artifact_dir", None) if self.logger else None

    def write_artifact(self, relative_path: str, content: str):
        root = self.artifact_root()
        if not root:
            return None
        path = os.path.join(root, relative_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content or "")
        return path

    def display_artifact_path(self, path: str):
        root = self.artifact_root()
        if not path or not root:
            return path
        return os.path.relpath(path, root)

    def safe_artifact_name(self, value: str) -> str:
        value = value or "unknown"
        value = re.sub(r"[^A-Za-z0-9_.@-]+", "_", value)
        return value.strip("_") or "unknown"

    def compact_text(self, text: str, limit: int = 300) -> str:
        text = re.sub(r"\s+", " ", str(text or "")).strip()
        if len(text) <= limit:
            return text
        return text[:limit - 3] + "..."

    def invariant_count(self) -> int:
        return sum(len(lp.loop_invariant) for lp in self.loop_list)

    def logger_inv(self):
        self.invariant_snapshot_count += 1
        lines = []
        for lp in self.loop_list:
            lines.append(f"Loop {lp.display_name}:")
            for k, v in lp.loop_invariant.items():
                 lines.append(f"  {k}: {v}")
        snapshot_path = self.write_artifact(
            f"invariants/{self.invariant_snapshot_count:03d}.txt",
            "\n".join(lines) + ("\n" if lines else ""),
        )
        counts = ", ".join(
            f"{lp.display_name}:{len(lp.loop_invariant)}"
            for lp in self.loop_list
        )
        self.logger.info(
            "[INVARIANTS] total=%d loops=%s snapshot=%s",
            self.invariant_count(),
            counts,
            self.display_artifact_path(snapshot_path),
        )
            
    def remove_all_invariants(self):
        for lp in self.loop_list:
            lp.loop_invariant.clear()

    def strip_loop_invariants_from_code(self, code: str) -> str:
        result = []
        pos = 0
        for m in re.finditer(r"\bloop\s+invariant\b", code, re.I):
            start = m.start()
            if start < pos:
                continue
            clause, end = scan_clause_from(code, start)
            if not clause:
                continue
            result.append(code[pos:start])
            pos = end
        result.append(code[pos:])
        return "".join(result)

    def llm_prompt_code_base(self) -> str:
        if self.ablation_mode:
            return self.strip_loop_invariants_from_code(self.code)
        return self.inject_invariants()
            
    def flatten_function_clauses(self, clauses_by_function: dict) -> dict:
        flattened = {}
        for func_name, clauses in clauses_by_function.items():
            for label, body in clauses.items():
                flattened.setdefault(label, body)
                flattened[f"{func_name}:{label}"] = body
        return flattened

    def extract_function_parameters(self, code: str) -> dict:
        result = {}
        for region in self.function_regions:
            header = code[region["name_start"]:region["body_start"]]
            open_pos = header.find("(")
            close_pos = header.rfind(")")
            if open_pos == -1 or close_pos == -1 or close_pos <= open_pos:
                result[region["name"]] = []
                continue

            params_text = header[open_pos + 1:close_pos].strip()
            if not params_text or params_text == "void":
                result[region["name"]] = []
                continue

            params = []
            for raw_param in split_top_level_commas(params_text):
                raw_param = raw_param.strip()
                if not raw_param or raw_param == "void":
                    continue
                names = re.findall(r"[A-Za-z_]\w*", raw_param)
                if names:
                    params.append(names[-1])
            result[region["name"]] = params
        return result

    def extract_file_contracts(self, code: str, keyword: str, include_asserts: bool = False) -> dict:
        return {
            region["name"]: self.extract_function_contracts(code, region, keyword, include_asserts)
            for region in self.function_regions
        }

    def extract_file_asserts(self, code: str) -> dict:
        return {
            region["name"]: self.extract_function_assert_clauses(code, region)
            for region in self.function_regions
        }

    def extract_function_assert_clauses(self, code: str, function_region: dict) -> list:
        func_body = code[function_region["body_start"]:function_region["body_end"]]
        assertions = []

        for m in re.finditer(r"//@\s*assert\b", func_body):
            clause, _ = scan_clause_from(func_body, m.end())
            if clause:
                assertions.append((m.start(), clause))

        for block in re.finditer(r"/\*@(?P<comment>.*?)\*/", func_body, re.DOTALL):
            comment = block.group("comment")
            for m in re.finditer(r"\bassert\b", comment):
                clause, _ = scan_clause_from(comment, m.end())
                if clause:
                    assertions.append((block.start() + m.start(), clause))

        assertions.sort(key=lambda item: item[0])
        return [clause for _, clause in assertions]

    def extract_function_contracts(self, code: str, function_region: dict, keyword: str, include_asserts: bool = False) -> dict:
        clauses_dict = {}
        auto_index = 1

        comment_blocks = []
        for m in re.finditer(r"/\*@(?P<comment>.*?)\*/", code, re.DOTALL):
            comment_blocks.append((m.start(), m.end(), m.group("comment")))

        selected_comment = None
        last_pos = -1
        for pos, end_pos, comment in comment_blocks:
            between = code[end_pos:function_region["name_start"]]
            separated_by_code = any(ch in between for ch in "{};")
            if pos < function_region["name_start"] and pos > last_pos and not separated_by_code:
                last_pos = pos
                selected_comment = comment

        if selected_comment:
            cb = selected_comment
            for m in re.finditer(r"\b" + re.escape(keyword) + r"\b", cb):
                abs_start = m.end()
                clause, _ = scan_clause_from(cb, abs_start)
                if clause is None:
                    continue

                lab_m = re.match(r"\s*([A-Za-z0-9_]+)\s*:\s*(.*)", clause, re.DOTALL)
                if lab_m:
                    label = lab_m.group(1)
                    body = lab_m.group(2).strip()
                else:
                    label = f"{keyword[0]}_{auto_index}"
                    body = clause.strip()
                    auto_index += 1

                body = re.sub(r"\s+", " ", body)
                clauses_dict[label] = body

        if include_asserts:
            for clause in self.extract_function_assert_clauses(code, function_region):
                lab_m = re.match(r"\s*([A-Za-z0-9_]+)\s*:\s*(.*)", clause, re.DOTALL)
                if lab_m:
                    label = lab_m.group(1)
                    body = lab_m.group(2).strip()
                else:
                    label = f"a_{auto_index}"
                    body = clause
                    auto_index += 1

                body = re.sub(r"\s+", " ", body)
                clauses_dict[label] = body

        return clauses_dict


    def update_error_count(self, error_msg):
        if self.error_count.get(error_msg) != None:
            self.error_count[error_msg] += 1
        elif self.error_count.get(error_msg) == None:
            self.error_count[error_msg] = 0
        
    
    def loop_graph_construct(self):
        parent = {}
        for lp in self.loop_list:
            if isinstance(lp.loop_state, list):  
                for child_idx in lp.loop_state:
                    parent[child_idx] = lp.index

        self.parent_by_loop = parent
        self.children_by_loop = {lp.index: [] for lp in self.loop_list}
        for child_idx, parent_idx in parent.items():
            self.children_by_loop.setdefault(parent_idx, []).append(child_idx)
        for children in self.children_by_loop.values():
            children.sort()

        layers = {}
        for lp in self.loop_list:
            p = parent.get(lp.index, None)
            layers.setdefault((lp.function_name, p), []).append(lp.index)


        for lp in self.loop_list:
            pid = parent.get(lp.index, None)
            same_layer = layers.get((lp.function_name, pid), [])
            idx_in_layer = same_layer.index(lp.index)


            if idx_in_layer == 0:
                lp.pre_loop = pid if pid is not None else -1
            else:
                lp.pre_loop = same_layer[idx_in_layer - 1]

            if isinstance(lp.loop_state, list) and lp.loop_state:
      
                inner_first = lp.loop_state[0]
                next_same = same_layer[idx_in_layer + 1] if idx_in_layer < len(same_layer) - 1 else "inf"
                post = [inner_first]
                if next_same is not None:
                    post.append(next_same)
                lp.post_loop = post
            else:
            
                if idx_in_layer < len(same_layer) - 1:
                    lp.post_loop = same_layer[idx_in_layer + 1]
                else:
                    lp.post_loop = pid if pid is not None else "inf"

    def node_name_for_loop(self, loop_index: int) -> str:
        lp = self.loop_list[loop_index]
        return lp.display_name

    def boundary_node(self, function_name: str, kind: str) -> str:
        return f"{kind}@{function_name}"

    def loops_in_function(self, function_name: str):
        return [lp for lp in self.loop_list if lp.function_name == function_name]

    def top_level_loops_in_function(self, function_name: str):
        return [
            lp for lp in self.loops_in_function(function_name)
            if self.parent_by_loop.get(lp.index) is None
        ]

    def find_innermost_loop_at(self, function_name: str, abs_pos: int):
        candidates = [
            lp for lp in self.loops_in_function(function_name)
            if lp.body_start_pos is not None
            and lp.body_end_pos is not None
            and lp.body_start_pos <= abs_pos <= lp.body_end_pos
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda lp: lp.body_end_pos - lp.body_start_pos)

    def transition_target_after_call(self, caller: str, abs_pos: int, owner_loop: Optional[Loop]):
        if owner_loop is not None:
            direct_children = [
                self.loop_list[idx] for idx in self.children_by_loop.get(owner_loop.index, [])
            ]
            following_children = [
                child for child in direct_children
                if child.body_start_pos is not None and abs_pos < child.body_start_pos
            ]
            if following_children:
                return min(following_children, key=lambda lp: lp.body_start_pos).index
            return owner_loop.index

        following_top_level = [
            lp for lp in self.top_level_loops_in_function(caller)
            if lp.body_start_pos is not None and abs_pos < lp.body_start_pos
        ]
        if following_top_level:
            return min(following_top_level, key=lambda lp: lp.body_start_pos).index
        return None

    def call_source_node(self, caller: str, owner_loop: Optional[Loop], transition_to_loop):
        if owner_loop is not None:
            return self.node_name_for_loop(owner_loop.index)

        if transition_to_loop is None:
            return self.boundary_node(caller, "Entry")

        top_level = self.top_level_loops_in_function(caller)
        if top_level and transition_to_loop == top_level[0].index:
            return self.boundary_node(caller, "Entry")

        previous = [
            lp for lp in top_level
            if lp.body_start_pos is not None
            and self.loop_list[transition_to_loop].body_start_pos is not None
            and lp.body_start_pos < self.loop_list[transition_to_loop].body_start_pos
        ]
        if previous:
            return self.node_name_for_loop(max(previous, key=lambda lp: lp.body_start_pos).index)
        return self.boundary_node(caller, "Entry")

    def line_text_at(self, abs_pos: int) -> str:
        line_start = self.raw_code.rfind("\n", 0, abs_pos) + 1
        line_end = self.raw_code.find("\n", abs_pos)
        if line_end == -1:
            line_end = len(self.raw_code)
        return self.raw_code[line_start:line_end].strip()

    def build_call_edges(self):
        function_names = [region["name"] for region in self.function_regions]
        if not function_names:
            self.call_edges = []
            return

        names_pattern = "|".join(re.escape(name) for name in sorted(function_names, key=len, reverse=True))
        call_re = re.compile(r"\b(?P<callee>" + names_pattern + r")\s*\(")
        call_edges = []

        for region in self.function_regions:
            caller = region["name"]
            body_start = region["body_start"]
            body = self.raw_code[region["body_start"]:region["body_end"]]
            masked_body = mask_comments_and_strings(body)

            for m in call_re.finditer(masked_body):
                callee = m.group("callee")
                abs_pos = body_start + m.start("callee")
                owner_loop = self.find_innermost_loop_at(caller, abs_pos)
                transition_to_loop = self.transition_target_after_call(caller, abs_pos, owner_loop)
                source_node = self.call_source_node(caller, owner_loop, transition_to_loop)
                target_node = (
                    self.node_name_for_loop(transition_to_loop)
                    if transition_to_loop is not None
                    else self.boundary_node(caller, "Exit")
                )

                call_edges.append({
                    "id": f"call_{len(call_edges)}",
                    "caller": caller,
                    "callee": callee,
                    "line": self.raw_code[:abs_pos].count("\n") + 1,
                    "text": self.line_text_at(abs_pos),
                    "position": abs_pos,
                    "source_loop": owner_loop.index if owner_loop is not None else None,
                    "source_node": source_node,
                    "transition_to_loop": transition_to_loop,
                    "transition_to_node": target_node,
                    "callee_entry": self.boundary_node(callee, "Entry"),
                    "callee_exit": self.boundary_node(callee, "Exit"),
                })

        self.call_edges = call_edges

    def function_contract_summary(self, function_name: str) -> str:
        clauses = []
        requires = self.requires_by_function.get(function_name, {})
        ensures = self.ensures_by_function.get(function_name, {})
        if requires:
            req_text = "; ".join(f"{k}: {v}" for k, v in requires.items())
            clauses.append(f"requires({function_name}): {req_text}")
        if ensures:
            ens_text = "; ".join(f"{k}: {v}" for k, v in ensures.items())
            clauses.append(f"ensures({function_name}): {ens_text}")
        return " | ".join(clauses)

    def format_call_context(self, call_edges, title: str) -> str:
        if not call_edges:
            return ""
        parts = [title]
        for edge in call_edges:
            summary = self.function_contract_summary(edge["callee"])
            if summary:
                parts.append(
                    f"call {edge['callee']} at line {edge['line']} ({edge['text']}): {summary}"
                )
            else:
                parts.append(
                    f"call {edge['callee']} at line {edge['line']} ({edge['text']})"
                )
        return " ".join(parts)

    def actual_arguments_for_call(self, edge) -> list:
        open_pos = self.raw_code.find("(", edge["position"])
        if open_pos == -1:
            return []
        close_pos = find_matching_paren(mask_comments_and_strings(self.raw_code), open_pos)
        if close_pos is None:
            return []
        return split_top_level_commas(self.raw_code[open_pos + 1:close_pos])

    def parenthesize_actual_arg(self, arg: str) -> str:
        arg = arg.strip()
        if re.fullmatch(r"[A-Za-z_]\w*(?:\[[^\]]+\])*", arg):
            return arg
        return f"({arg})"

    def instantiate_clause_with_call_args(self, clause: str, callee: str, args: list) -> str:
        params = self.parameters_by_function.get(callee, [])
        result = clause
        replacements = {}
        for idx, (param, arg) in enumerate(zip(params, args)):
            placeholder = f"__COL2INV_ARG_{idx}__"
            replacements[placeholder] = self.parenthesize_actual_arg(arg)
            result = re.sub(
                r"\b" + re.escape(param) + r"\b",
                placeholder,
                result,
            )
        for placeholder, arg in replacements.items():
            result = result.replace(placeholder, arg)
        return result

    def instantiated_requires_for_call(self, edge) -> dict:
        requires = self.requires_by_function.get(edge["callee"], {})
        args = self.actual_arguments_for_call(edge)
        return {
            req_id: self.instantiate_clause_with_call_args(req, edge["callee"], args)
            for req_id, req in requires.items()
        }

    def statement_start_before(self, abs_pos: int) -> int:
        i = abs_pos - 1
        while i >= 0 and self.raw_code[i].isspace():
            i -= 1
        while i >= 0:
            if self.raw_code[i] in ";{}":
                return i + 1
            i -= 1
        return 0

    def inner_state_before_call(self, current_loop: Loop, edge) -> str:
        start = current_loop.body_content_start_pos
        if start is None:
            return "skip"

        call_statement_start = self.statement_start_before(edge["position"])
        if call_statement_start < start:
            call_statement_start = edge["position"]

        state = clean_code_snippet(self.raw_code[start:call_statement_start])
        return state or "skip"

    def build_inner_call_obligations(self, current_loop: Loop) -> list:
        obligations = []
        for edge in self.body_call_edges_for_loop(current_loop.index):
            requires = self.instantiated_requires_for_call(edge)
            requires_context = self.format_clause_context(requires, "true")
            obligations.append({
                "kind": "inner_call_requires",
                "callee": edge["callee"],
                "line": edge["line"],
                "call": edge["text"],
                "precondition": f"I && ( {current_loop.condition or 'true'} )",
                "state": self.inner_state_before_call(current_loop, edge),
                "postcondition": requires_context,
            })
        return obligations

    def format_obligation_triples(self, obligations: list) -> str:
        if not obligations:
            return ""

        parts = []
        for obligation in obligations:
            if obligation["kind"] == "loop_exit":
                parts.append(
                    f"    - {{ {obligation['precondition']} }} "
                    f"{obligation['state']} "
                    f"{{ {obligation['postcondition']} }}: "
                    "when the current loop exits, the generated invariant together "
                    "with the negated loop condition should entail the obligation "
                    "context after the post-state transition."
                )
            elif obligation["kind"] == "inner_call_requires":
                parts.append(
                    f"    - {{ {obligation['precondition']} }} "
                    f"{obligation['state']} "
                    f"{{ {obligation['postcondition']} }}: "
                    f"when execution reaches the call to {obligation['callee']} "
                    f"at line {obligation['line']} ({obligation['call']}), "
                    "the generated invariant together with the loop condition "
                    "should entail the callee's requires clauses after the "
                    "inner-state transition."
                )
        return "\n".join(parts)

    def append_context(self, base: str, extra: str) -> str:
        if base and extra:
            return f"{base} {extra}"
        return base or extra

    def incoming_call_edges_for_loop(self, loop_index: int):
        return [
            edge for edge in self.call_edges
            if edge.get("transition_to_loop") == loop_index
            and edge.get("source_loop") != loop_index
        ]

    def body_call_edges_for_loop(self, loop_index: int):
        return [
            edge for edge in self.call_edges
            if edge.get("source_loop") == loop_index
        ]

    def format_clause_context(self, clauses: dict, empty: str = "") -> str:
        if not clauses:
            return empty
        return "; ".join(f"{key}: {value}" for key, value in clauses.items())

    def format_loop_invariant_context(self, loop_index: int) -> str:
        lp = self.loop_list[loop_index]
        if not lp.loop_invariant:
            return ""
        invariants = "; ".join(
            f"{inv_id}: {inv}" for inv_id, inv in lp.loop_invariant.items()
        )
        return f"Loop {lp.display_name} invariants: {invariants}"

    def incoming_transition_edge_for_loop(self, loop_index: int):
        target = self.node_name_for_loop(loop_index)
        for edge in self.transition_edges:
            if edge["target"] == target and edge["kind"] == "establish":
                return edge
        return None

    def initial_post_transition_edge_for_loop(self, loop_index: int):
        lp = self.loop_list[loop_index]
        source = self.node_name_for_loop(loop_index)
        candidates = [
            edge for edge in self.transition_edges
            if edge["source"] == source and edge["kind"] != "self"
        ]
        if not candidates:
            return None

        if self.children_by_loop.get(loop_index):
            for edge in candidates:
                if edge["kind"] == "exit":
                    return edge

        if self.parent_by_loop.get(loop_index) is not None:
            for edge in candidates:
                if edge["kind"] == "back":
                    return edge

        for preferred in ("establish", "exit", "back"):
            for edge in candidates:
                if edge["kind"] == preferred:
                    return edge
        return candidates[0]

    def context_for_transition_source(self, edge, current_loop: Loop) -> str:
        if edge is None:
            requires = self.requires_by_function.get(current_loop.function_name, {})
            return self.format_clause_context(requires, "the function's requires clauses")

        if edge.get("source_loop") is not None:
            return self.format_loop_invariant_context(edge["source_loop"])

        requires = self.requires_by_function.get(current_loop.function_name, {})
        return self.format_clause_context(requires, "the function's requires clauses")

    def context_for_transition_target(self, edge, current_loop: Loop) -> str:
        if edge is None:
            ensures = self.ensures_by_function.get(current_loop.function_name, {})
            return self.format_clause_context(ensures, "the function's ensures clauses")

        if edge.get("target_loop") is not None:
            return self.format_loop_invariant_context(edge["target_loop"])

        ensures = self.ensures_by_function.get(current_loop.function_name, {})
        return self.format_clause_context(ensures, "the function's ensures clauses")

    def build_obligation_guided_context(self, current_loop: Loop) -> dict:
        incoming_edge = self.incoming_transition_edge_for_loop(current_loop.index)
        post_edge = self.initial_post_transition_edge_for_loop(current_loop.index)

        assumption_context = self.context_for_transition_source(incoming_edge, current_loop)
        obligation_context = self.context_for_transition_target(post_edge, current_loop)

        incoming_calls = self.format_call_context(
            self.incoming_call_edges_for_loop(current_loop.index),
            "Function calls on the incoming transition:"
        )

        assumption_context = self.append_context(assumption_context, incoming_calls)

        loop_condition = current_loop.condition or "true"
        post_state = post_edge["statement"] if post_edge else current_loop.post_state
        obligation_context = obligation_context or "true"
        proof_obligations = [{
            "kind": "loop_exit",
            "precondition": f"I && ! ( {loop_condition} )",
            "state": post_state or "skip",
            "postcondition": obligation_context,
        }]
        proof_obligations.extend(self.build_inner_call_obligations(current_loop))

        return {
            "assumption_context": assumption_context or "true",
            "pre_state": incoming_edge["statement"] if incoming_edge else current_loop.pre_state,
            "obligation_context": obligation_context,
            "post_state": post_state,
            "loop_condition": loop_condition,
            "obligation_triples": self.format_obligation_triples(proof_obligations),
            "proof_obligations": proof_obligations,
        }

    def loop_node_or_boundary(self, loop_index):
        if loop_index == -1 or loop_index is None:
            return None
        return self.node_name_for_loop(loop_index)

    def parent_back_transition(self, child_idx: int, parent_idx: int) -> str:
        child = self.loop_list[child_idx]
        parent = self.loop_list[parent_idx]
        between = ""
        if child.body_end_pos is not None and parent.body_content_end_pos is not None:
            between = clean_code_snippet(
                self.raw_code[child.body_end_pos + 1:parent.body_content_end_pos]
            )
        return join_statements(between, parent.update_state)

    def build_transition_edges(self):
        edges = []

        for lp in self.loop_list:
            if lp.pre_loop == -1:
                source = self.boundary_node(lp.function_name, "Entry")
            else:
                source = self.node_name_for_loop(lp.pre_loop)

            edges.append({
                "kind": "establish",
                "source": source,
                "target": self.node_name_for_loop(lp.index),
                "statement": lp.pre_state,
                "source_loop": lp.pre_loop if lp.pre_loop != -1 else None,
                "target_loop": lp.index,
            })

        for lp in self.loop_list:
            children = self.children_by_loop.get(lp.index, [])

            if not children and lp.loop_state:
                edges.append({
                    "kind": "self",
                    "source": self.node_name_for_loop(lp.index),
                    "target": self.node_name_for_loop(lp.index),
                    "statement": lp.loop_state,
                    "source_loop": lp.index,
                    "target_loop": lp.index,
                })

            parent_idx = self.parent_by_loop.get(lp.index)
            siblings = []
            if parent_idx is not None:
                siblings = self.children_by_loop.get(parent_idx, [])
            else:
                siblings = [x.index for x in self.top_level_loops_in_function(lp.function_name)]

            is_last_sibling = not siblings or siblings[-1] == lp.index
            if parent_idx is not None and is_last_sibling:
                edges.append({
                    "kind": "back",
                    "source": self.node_name_for_loop(lp.index),
                    "target": self.node_name_for_loop(parent_idx),
                    "statement": self.parent_back_transition(lp.index, parent_idx),
                    "source_loop": lp.index,
                    "target_loop": parent_idx,
                })

            if parent_idx is None and is_last_sibling:
                edges.append({
                    "kind": "exit",
                    "source": self.node_name_for_loop(lp.index),
                    "target": self.boundary_node(lp.function_name, "Exit"),
                    "statement": lp.post_state,
                    "source_loop": lp.index,
                    "target_loop": None,
                })

        self.transition_edges = edges

    def add_loop(self, loop: Loop):
        self.loop_list.append(loop)
    
    def remove_invariant_by_id(self, invid):
        loopidx = self.search_invariant_by_id(invid)
        if loopidx != None:
            self.loop_list[loopidx].loop_invariant.pop(invid, f'No Loop Invariant \"{invid}\" in C program !')

    def search_invariant_by_id(self, invid):
        for lp in self.loop_list:
            loop_inv_list: dict = lp.loop_invariant
            if loop_inv_list.get(invid) != None:
                return lp.index
        return None

    def search_invariant_content_by_id(self, invid):
        for lp in self.loop_list:
            loop_inv_list: dict = lp.loop_invariant
            if loop_inv_list.get(invid) != None:
                return loop_inv_list.get(invid)
        return None

    def is_contract_goal_id(self, goal_id: str) -> bool:
        local_id = goal_id.split(":")[-1]
        return local_id.startswith("e") or local_id.startswith("a")

    def contract_goal_function(self, goal_id: str):
        if ":" in goal_id:
            return goal_id.split(":", 1)[0]
        return None

    def last_loop_in_function(self, function_name: str):
        if function_name:
            for lp in reversed(self.loop_list):
                if lp.function_name == function_name:
                    return lp
        return self.loop_list[-1] if self.loop_list else None

    def get_llm_answer(self, promptcontent, prompt_type: str = "llm", current_loop: Loop = None):
        self.llm_call_count += 1
        loop_name = current_loop.display_name if current_loop else "global"
        call_tag = (
            f"{self.llm_call_count:03d}_"
            f"{self.safe_artifact_name(prompt_type)}_"
            f"{self.safe_artifact_name(loop_name)}"
        )
        prompt_path = self.write_artifact(
            f"llm_calls/{call_tag}.prompt.txt",
            promptcontent,
        )
        if not self.local_model_context:
            api_provider, base_url, api_key, api_model = get_api_client_config(self.model)
            client = openai.OpenAI(
                base_url=base_url,
                api_key=api_key,
                timeout=240.0,
                max_retries=1,
            )
            try:
                completion = client.chat.completions.create(
                    extra_body={"thinking": {"type": "enabled"}},
                    model=api_model,
                    messages=[{"role": "user", "content": promptcontent}],
                )
                llm_content = completion.choices[0].message.content or ""
            except Exception as exc:
                llm_content = ""
                response_path = self.write_artifact(
                    f"llm_calls/{call_tag}.response.txt",
                    f"[LLM_ERROR] {type(exc).__name__}: {exc}",
                )
                self.logger.warning(
                    "[LLM %03d] type=%s loop=%s failed=%s response=%s",
                    self.llm_call_count,
                    prompt_type,
                    loop_name,
                    self.compact_text(f"{type(exc).__name__}: {exc}", 220),
                    self.display_artifact_path(response_path),
                )
                return llm_content

            response_path = self.write_artifact(
                f"llm_calls/{call_tag}.response.txt",
                llm_content,
            )
            self.logger.info(
                "[LLM %03d] provider=%s api_model=%s type=%s loop=%s prompt=%s response=%s response_chars=%d",
                self.llm_call_count,
                api_provider,
                api_model,
                prompt_type,
                loop_name,
                self.display_artifact_path(prompt_path),
                self.display_artifact_path(response_path),
                len(llm_content or ""),
            )
            self.logger.info(
                "[LLM %03d] response_preview=%s",
                self.llm_call_count,
                self.compact_text(llm_content, 220),
            )
            return llm_content
        
        local_tokenizer = self.local_model_context["tokenizer"]
        local_model = self.local_model_context["model"]
        messages = [
            {"role": "system", "content": prompt.sys_prompt},
            {"role": "user", "content": promptcontent},
        ]
        text = local_tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )
        model_device = getattr(local_model, "device", None)
        if model_device is None:
            model_device = next(local_model.parameters()).device
        model_inputs = local_tokenizer([text], return_tensors="pt").to(model_device)
        generate_kwargs = {
            "max_new_tokens": self.local_model_context.get("max_new_tokens", 32768),
            "return_dict_in_generate": True,
        }
        eos_token_id = self.local_model_context.get("eos_token_id")
        if eos_token_id is not None:
            generate_kwargs["eos_token_id"] = eos_token_id
        outputs = local_model.generate(**model_inputs, **generate_kwargs)
        input_length = model_inputs.input_ids.shape[1]
        generated_tokens = outputs.sequences[:, input_length:]
        content = local_tokenizer.decode(generated_tokens[0], skip_special_tokens=True)
        response_path = self.write_artifact(
            f"llm_calls/{call_tag}.response.txt",
            content,
        )
        self.logger.info(
            "[LLM %03d] provider=local path=%s type=%s loop=%s prompt=%s response=%s response_chars=%d",
            self.llm_call_count,
            self.local_model_context.get("path", self.model),
            prompt_type,
            loop_name,
            self.display_artifact_path(prompt_path),
            self.display_artifact_path(response_path),
            len(content or ""),
        )
        self.logger.info(
            "[LLM %03d] response_preview=%s",
            self.llm_call_count,
            self.compact_text(content, 220),
        )
        return content


    def extract_loop_invariants(self, text: str):
        if text is None:
            text = ""
        """
        解析 LLM 输出两种格式：
        - 纯条目列表： loop invariant ...;
        - 分块形式： [Loop A] ... [Loop B] ...
        返回:
        - 若无分块 -> list of invariants (strings)
        - 若有分块 -> dict { "A": [inv1, inv2], ... }
        """

        def clean_invariant_clause(clause: str) -> str:
            clause = clause.strip()
            clause = re.split(r"```|\[Loop\b", clause, maxsplit=1)[0].strip()
            semicolon_pos = clause.find(";")
            colon_pos = clause.find(":")
            if colon_pos != -1 and (semicolon_pos == -1 or colon_pos < semicolon_pos):
                prefix = clause[:colon_pos]
                if 0 < len(prefix) <= 80:
                    clause = clause[colon_pos + 1:].strip()
            clause = re.sub(r"\s+", " ", clause).strip()
            if (
                not clause
                or any(ord(ch) > 127 for ch in clause)
                or "`" in clause
                or "[Loop" in clause
                or re.search(r"\bloop\s+invariant\b", clause, re.I)
            ):
                return ""
            return clause

        text = text.strip()
        # detect blocks first
        # split into tokens while preserving block headers
        block_matches = list(re.finditer(r"\[Loop\s+([A-Za-z0-9_@]+)\]", text))
        if not block_matches:
            # scan all occurrences of "loop invariant"
            res = []
            for m in re.finditer(r"loop\s+invariant", text):
                start = m.end()
                clause, nextpos = scan_clause_from(text, start)
                if clause:
                    clause = clean_invariant_clause(clause)
                    if clause:
                        res.append(clause)
            return res

        # has blocks: iterate through blocks and text regions between them
        result = {}
        # We'll walk through block_matches and extract region after each header up to next header or EOF
        n = len(text)
        for idx, m in enumerate(block_matches):
            block_name = m.group(1)
            start_region = m.end()
            end_region = block_matches[idx + 1].start() if idx + 1 < len(block_matches) else n
            region_text = text[start_region:end_region]
            # in region_text find all loop invariant clauses
            invariants = []
            # search for 'loop invariant' occurrences inside region_text, but need absolute index
            base = start_region
            for mm in re.finditer(r"loop\s+invariant", region_text):
                abs_start = base + mm.end()
                clause, _ = scan_clause_from(text, abs_start)
                if clause:
                    clause = clean_invariant_clause(clause)
                    if clause:
                        invariants.append(clause)
            result[block_name] = invariants
        return result

    def extract_failure_diagnosis(self, text: str) -> dict:
        def strip_code_fence(value: str) -> str:
            value = value.strip()
            value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
            value = re.sub(r"\s*```$", "", value)
            return value.strip()

        fields = {
            "failed_property": "",
            "failure_cause": "",
            "responsible_scope": "",
            "refinement_direction": "",
            "raw": text.strip(),
        }
        cleaned_text = strip_code_fence(text)
        try:
            obj = json.loads(cleaned_text)
            if isinstance(obj, dict):
                obj = {
                    str(k).strip().strip("[]").strip(): v
                    for k, v in obj.items()
                }
                fields["failed_property"] = str(
                    obj.get("Failed Property", obj.get("failed_property", ""))
                ).strip()
                fields["failure_cause"] = str(
                    obj.get("Failure Cause", obj.get("failure_cause", ""))
                ).strip()
                fields["responsible_scope"] = str(
                    obj.get("Responsible Scope", obj.get("responsible_scope", ""))
                ).strip()
                fields["refinement_direction"] = str(
                    obj.get("Refinement Direction", obj.get("refinement_direction", ""))
                ).strip()
                if fields["failed_property"] or fields["failure_cause"] or fields["responsible_scope"] or fields["refinement_direction"]:
                    if not fields["failure_cause"]:
                        fields["failure_cause"] = cleaned_text
                    if not fields["responsible_scope"]:
                        fields["responsible_scope"] = "current loop"
                    if not fields["refinement_direction"]:
                        fields["refinement_direction"] = "Refine the responsible loop invariants so that the failed proof obligation can be discharged."
                    return fields
        except json.JSONDecodeError:
            pass

        field_re = re.compile(
            r"\[(Failed Property|Failure Cause|Responsible Scope|Refinement Direction)\]\s*"
            r"(.*?)(?=\s*\[(?:Failed Property|Failure Cause|Responsible Scope|Refinement Direction)\]|\Z)",
            re.S | re.I,
        )
        key_map = {
            "failed property": "failed_property",
            "failure cause": "failure_cause",
            "responsible scope": "responsible_scope",
            "refinement direction": "refinement_direction",
        }
        for match in field_re.finditer(cleaned_text):
            key = key_map[match.group(1).lower()]
            fields[key] = strip_code_fence(match.group(2))

        if not fields["failure_cause"]:
            fields["failure_cause"] = cleaned_text
        if not fields["responsible_scope"]:
            fields["responsible_scope"] = "current loop"
        if not fields["refinement_direction"]:
            fields["refinement_direction"] = "Refine the responsible loop invariants so that the failed proof obligation can be discharged."
        return fields

    def describe_failed_property(self, error_msg_key: str, error_msg_value, failure_kind: str) -> str:
        if isinstance(error_msg_value, dict):
            parsed_property = error_msg_value.get("property", "")
            if parsed_property:
                normalized_property = str(parsed_property).strip().lower()
                if normalized_property == "establishment":
                    failure_kind = "establishment"
                elif normalized_property == "preservation":
                    failure_kind = "preservation"
                elif set(part.strip() for part in normalized_property.split(",")) == {"establishment", "preservation"}:
                    failure_kind = "both"
                elif normalized_property == "post-condition":
                    return (
                        f"post-condition {error_msg_key}: after the relevant loop exits, "
                        "the available invariants and the negated loop condition must imply "
                        "the function ensures clause."
                    )
                elif normalized_property == "assertion":
                    return (
                        f"assertion {error_msg_key}: at the assertion point, the available "
                        "program state and loop invariants must imply the asserted property."
                    )
                else:
                    return str(parsed_property)

        if failure_kind == "establishment":
            return (
                f"establishment of {error_msg_key}: the invariant must hold before the "
                "first loop iteration from the incoming program state."
            )
        if failure_kind == "preservation":
            return (
                f"preservation of {error_msg_key}: assuming the invariant and loop "
                "condition before one iteration, the invariant must hold again after "
                "the loop body."
            )
        if failure_kind == "both":
            return (
                f"establishment and preservation of {error_msg_key}: the invariant must "
                "hold both at loop entry and after each loop-body iteration."
            )

        local_key = error_msg_key.split(":")[-1]
        if local_key.startswith("e"):
            return (
                f"post-condition {error_msg_key}: after the relevant loop exits, the "
                "available invariants and the negated loop condition must imply the "
                "function ensures clause."
            )
        if local_key.startswith("a"):
            return (
                f"assertion {error_msg_key}: at the assertion point, the available "
                "program state and loop invariants must imply the asserted property."
            )
        return f"{failure_kind or 'unknown'} proof obligation for {error_msg_key}"

    def dependency_loop_for_failure(self, current_loop: Loop, failure_kind: str):
        if failure_kind in ("preservation", "both"):
            if isinstance(current_loop.loop_state, list) and current_loop.loop_state:
                return self.loop_list[current_loop.loop_state[-1]]
        if failure_kind in ("establishment", "both"):
            if current_loop.pre_loop != -1:
                return self.loop_list[current_loop.pre_loop]
        return current_loop

    def diagnosis_available_invariants(self, current_loop: Loop, dependency_loop: Loop, failure_kind: str) -> str:
        if failure_kind == "establishment" and current_loop.pre_loop == -1:
            requires = self.requires_by_function.get(current_loop.function_name, {})
            return self.format_clause_context(requires, "true")
        if dependency_loop is None:
            return "true"
        return self.format_loop_invariant_context(dependency_loop.index) or "true"

    def diagnosis_dependency_condition(self, current_loop: Loop, dependency_loop: Loop, failure_kind: str) -> str:
        if failure_kind == "establishment" and current_loop.pre_loop == -1:
            return "false"
        if (
            failure_kind == "preservation"
            and dependency_loop is not None
            and dependency_loop.index == current_loop.index
            and not isinstance(current_loop.loop_state, list)
        ):
            return f"!({current_loop.condition or 'true'})"
        if dependency_loop is None:
            return "true"
        return dependency_loop.condition or "true"

    def normalize_repair_scope(self, diagnosis: dict, current_loop: Loop, dependency_loop: Loop):
        scope_text = diagnosis.get("responsible_scope", "")
        lowered = scope_text.lower()
        loops = []

        def add(lp):
            if lp is not None and lp.index not in [x.index for x in loops]:
                loops.append(lp)

        current_names = [
            current_loop.display_name.lower(),
            "cur_loop",
            "current loop",
            "failed invariant",
        ]
        dependency_names = []
        if dependency_loop is not None:
            dependency_names = [
                dependency_loop.display_name.lower(),
                "inner_loop",
                "inner loop",
                "related loop",
                "preceding loop",
                "pre_loop",
            ]

        def mentions_loop_alphabet(lp):
            if lp is None:
                return False
            return re.search(
                r"\bloop\s+" + re.escape(lp.alphabet.lower()) + r"\b",
                lowered,
            ) is not None

        if "both" in lowered:
            add(current_loop)
            if dependency_loop is not None and dependency_loop.index != current_loop.index:
                add(dependency_loop)
        else:
            if any(name in lowered for name in dependency_names) or mentions_loop_alphabet(dependency_loop):
                add(dependency_loop)
            if any(name in lowered for name in current_names) or mentions_loop_alphabet(current_loop):
                add(current_loop)

        if not loops:
            add(current_loop)
        return loops

    def repair_scope_label(self, repair_scope: list) -> str:
        return ", ".join(lp.display_name for lp in repair_scope)

    def repair_scope_block_format(self, repair_scope: list) -> str:
        if not repair_scope:
            return "[Loop CurrentLoop]"
        return "\n\n".join(f"[Loop {lp.display_name}]" for lp in repair_scope)

    def get_failure_diagnosis(self, current_loop: Loop, dependency_loop: Loop, error_invariant: str, wp_goal: str, failure_kind: str, failure_property: str) -> dict:
        prompt_content = prompt.get_prompt(
            prompt_code=self.llm_prompt_code_base(),
            prompt_type="failure_diagnosis",
            current_loop=current_loop,
            inner_loop=dependency_loop,
            error_msg=error_invariant,
            failure_property=failure_property,
            error_wp=wp_goal,
            inner_loop_invariants=self.diagnosis_available_invariants(current_loop, dependency_loop, failure_kind),
            inner_loop_condition=self.diagnosis_dependency_condition(current_loop, dependency_loop, failure_kind),
        )
        self.logger.info(
            "[DIAGNOSE] loop=%s dependency=%s target=%s property=%s wp=%s",
            current_loop.display_name if current_loop else "unknown",
            dependency_loop.display_name if dependency_loop else "Boundary",
            error_invariant,
            failure_property,
            self.compact_text(wp_goal, 180),
        )
        llm_response = self.get_llm_answer(
            prompt_content,
            prompt_type="failure_diagnosis",
            current_loop=current_loop,
        )
        diagnosis = self.extract_failure_diagnosis(llm_response)
        if diagnosis.get("failed_property") and diagnosis["failed_property"] != failure_property:
            diagnosis["model_failed_property"] = diagnosis["failed_property"]
        diagnosis["failed_property"] = failure_property
        self.logger.info(
            "[DIAGNOSIS] property=%s scope=%s cause=%s direction=%s",
            diagnosis.get("failed_property", ""),
            diagnosis.get("responsible_scope", ""),
            self.compact_text(diagnosis.get("failure_cause", ""), 240),
            self.compact_text(diagnosis.get("refinement_direction", ""), 240),
        )
        if diagnosis.get("model_failed_property"):
            self.logger.info(
                "[DIAGNOSIS] model_failed_property_overridden=%s",
                self.compact_text(diagnosis.get("model_failed_property", ""), 180),
            )
        return diagnosis

    def get_dependency_refinement_answer(self, current_loop: Loop, repair_scope: list, error_invariant: str, diagnosis: dict):
        hints = {
            lp.index: "[Hint] Please refine the loop invariants in the repair scope."
            for lp in repair_scope
        }
        code_with_hint = self.inject_reasoning_prompt_code_multi(hints)
        scope_label = self.repair_scope_label(repair_scope)
        prompt_content = prompt.get_prompt(
            prompt_code=code_with_hint,
            prompt_type="dependency_refinement",
            current_loop=current_loop,
            error_msg=error_invariant,
            failure_property=diagnosis.get("failed_property", ""),
            failure_cause=diagnosis.get("failure_cause", ""),
            responsible_scope=scope_label,
            refinement_direction=diagnosis.get("refinement_direction", ""),
            loop_in_scope=self.repair_scope_block_format(repair_scope),
        )
        self.logger.info(
            "[REFINE] failed=%s scope=%s property=%s",
            error_invariant,
            scope_label,
            diagnosis.get("failed_property", ""),
        )
        llm_response = self.get_llm_answer(
            prompt_content,
            prompt_type="dependency_refinement",
            current_loop=current_loop,
        )
        dict_or_list = self.extract_loop_invariants(llm_response)
        if isinstance(dict_or_list, dict):
            return dict_or_list
        if isinstance(dict_or_list, list):
            target_loop = repair_scope[0] if repair_scope else current_loop
            return {target_loop.display_name: dict_or_list}
        return {}

    def format_batch_feedback_errors(self, error_msg: dict) -> tuple[str, dict]:
        lines = []
        target_loops = {}
        for idx, (goal_id, value) in enumerate(error_msg.items(), start=1):
            target_loop = None
            if self.is_contract_goal_id(goal_id):
                target_loop = self.last_loop_in_function(self.contract_goal_function(goal_id))
                failure_property = self.describe_failed_property(goal_id, value, "contract")
            elif str(goal_id).startswith("i"):
                loop_index = self.search_invariant_by_id(goal_id)
                if loop_index is not None:
                    target_loop = self.loop_list[loop_index]
                failure_kind = value.get("status", "unknown") if isinstance(value, dict) else "unknown"
                if failure_kind in ("establishment", "preservation"):
                    failure_property = f"{failure_kind} of {goal_id}"
                elif failure_kind == "both":
                    failure_property = f"establishment and preservation of {goal_id}"
                else:
                    failure_property = self.describe_failed_property(goal_id, value, failure_kind).split(":", 1)[0]
            else:
                failure_property = self.describe_failed_property(goal_id, value, "unknown").split(":", 1)[0]

            if target_loop is not None:
                target_loops[target_loop.index] = target_loop

            lines.append(
                "\n".join(
                    [
                        f"[Error {idx}]",
                        f"Loop: {target_loop.display_name if target_loop else 'unknown'}",
                        failure_property,
                    ]
                )
            )
        return "\n\n".join(lines) if lines else "No parsed verification errors.", target_loops

    def batch_feedback_refine_failure(self, error_msg: dict):
        error_text, target_loops = self.format_batch_feedback_errors(error_msg)
        hints = {
            lp.index: "[Hint] Please refine this loop invariant using the batch Frama-C error feedback."
            for lp in target_loops.values()
        }
        if not hints:
            hints = {
                lp.index: "[Hint] Please refine this loop invariant using the batch Frama-C error feedback."
                for lp in self.loop_list
            }
        code_with_hint = self.inject_reasoning_prompt_code_multi(hints)
        prompt_content = prompt.get_prompt(
            prompt_code=code_with_hint,
            prompt_type="batch_feedback_refinement_ablation",
            error_msg=error_text,
        )
        self.logger.info(
            "[ABLATION-BATCH] errors=%d target_loops=%s",
            len(error_msg),
            ", ".join(lp.display_name for lp in target_loops.values()) if target_loops else "all",
        )
        llm_response = self.get_llm_answer(
            prompt_content,
            prompt_type="batch_feedback_refinement_ablation",
        )
        dict_or_list = self.extract_loop_invariants(llm_response)
        if isinstance(dict_or_list, dict):
            self.apply_llm_invariants(dict_or_list)
        elif isinstance(dict_or_list, list) and len(target_loops) == 1:
            target_loop = next(iter(target_loops.values()))
            self.apply_llm_invariants({target_loop.display_name: dict_or_list})
        else:
            self.logger.info("[ABLATION-BATCH] no block-form invariant candidates extracted")

    def diagnose_and_refine_failure(self, current_loop: Loop, error_invariant: str, wp_goal: str, failure_kind: str, failure_property: str):
        dependency_loop = self.dependency_loop_for_failure(current_loop, failure_kind)
        diagnosis = self.get_failure_diagnosis(
            current_loop=current_loop,
            dependency_loop=dependency_loop,
            error_invariant=error_invariant,
            wp_goal=wp_goal,
            failure_kind=failure_kind,
            failure_property=failure_property,
        )
        repair_scope = self.normalize_repair_scope(diagnosis, current_loop, dependency_loop)
        llm_reasoning_answer = self.get_dependency_refinement_answer(
            current_loop=current_loop,
            repair_scope=repair_scope,
            error_invariant=error_invariant,
            diagnosis=diagnosis,
        )
        self.apply_llm_invariants(llm_reasoning_answer)


    def get_initial_loop_invariants(self, current_loop: Loop):
        code_with_hint = self.inject_reasoning_prompt_code(
            loop_index=current_loop.index,
            hint="[Hint] Please infer the loop invariant for the following loop.",
        )
        obligation_context = self.build_obligation_guided_context(current_loop)
        prompt_content = prompt.get_prompt(
            prompt_code=code_with_hint,
            prompt_type="obligation_guided_initial",
            current_loop=current_loop,
            assumption_context=obligation_context["assumption_context"],
            pre_state=obligation_context["pre_state"],
            obligation_triples=obligation_context["obligation_triples"],
        )

        llm_response = self.get_llm_answer(
            prompt_content,
            prompt_type="initial_reasoning",
            current_loop=current_loop,
        )
        dict_or_list = self.extract_loop_invariants(llm_response)
        if isinstance(dict_or_list, dict):
            return dict_or_list
        elif isinstance(dict_or_list, list):
            return {str(current_loop.alphabet) : dict_or_list}

    def get_plain_initial_loop_invariants(self, current_loop: Loop):
        code_with_hint = self.inject_reasoning_prompt_code(
            loop_index=current_loop.index,
            hint="[Hint] Please infer the loop invariant for the following loop.",
        )
        prompt_content = prompt.get_prompt(
            prompt_code=code_with_hint,
            prompt_type="initial_reasoning_ablation",
            current_loop=current_loop,
        )

        llm_response = self.get_llm_answer(
            prompt_content,
            prompt_type="initial_reasoning_ablation",
            current_loop=current_loop,
        )
        dict_or_list = self.extract_loop_invariants(llm_response)
        if isinstance(dict_or_list, dict):
            return dict_or_list
        elif isinstance(dict_or_list, list):
            return {str(current_loop.alphabet): dict_or_list}

    def iter_initial_loop_order(self):
        def boundary_inward(loops):
            ordered = sorted(loops, key=lambda lp: lp.body_start_pos or 0)
            n = len(ordered)
            for i in range(n):
                if i % 2 == 0:
                    yield ordered[i // 2]
                else:
                    yield ordered[n - 1 - (i // 2)]

        def loop_depth(loop_index):
            depth = 0
            parent = self.parent_by_loop.get(loop_index)
            while parent is not None:
                depth += 1
                parent = self.parent_by_loop.get(parent)
            return depth

        for region in self.function_regions:
            function_loops = self.loops_in_function(region["name"])
            if not function_loops:
                continue

            depths = {lp.index: loop_depth(lp.index) for lp in function_loops}
            for depth in range(max(depths.values()) + 1):
                groups = {}
                for lp in function_loops:
                    if depths[lp.index] != depth:
                        continue
                    groups.setdefault(self.parent_by_loop.get(lp.index), []).append(lp)

                for parent_idx in sorted(
                    groups,
                    key=lambda idx: -1 if idx is None else (self.loop_list[idx].body_start_pos or 0)
                ):
                    for lp in boundary_inward(groups[parent_idx]):
                        yield lp

    def initial_loop_invariant_inference(self):
        for current_loop in self.iter_initial_loop_order():
            llm_reasoning_answer = self.get_initial_loop_invariants(current_loop)
            self.apply_llm_invariants(llm_reasoning_answer)

    def initial_loop_invariant_inference_ablation(self):
        for current_loop in self.iter_initial_loop_order():
            llm_reasoning_answer = self.get_plain_initial_loop_invariants(current_loop)
            self.apply_llm_invariants(llm_reasoning_answer)

    def apply_llm_invariants(self, llm_answer_dict):
        """
        llm_answer_dict 形如:
        {
            "A": ["inv1", "inv2"],
            "C": ["inv3"]
        }
        """
        if not llm_answer_dict:
            self.logger.info("[APPLY] no invariant candidates extracted")
            return

        letter2id = {}
        for lp in self.loop_list:
            letter2id[lp.alphabet] = lp.index
            letter2id[lp.display_name] = lp.index
        for letter, inv_list in llm_answer_dict.items():
            if letter not in letter2id:
                self.logger.info("[APPLY] skip unknown loop block=%s", letter)
                continue
            loop_index = letter2id[letter]
            loop_obj: Loop = self.loop_list[loop_index]
            added = []
            for inv in inv_list:
                if inv in loop_obj.loop_invariant.values():
                    continue
                inv_id = f"i_{self.invid}"
                loop_obj.loop_invariant[inv_id] = inv
                self.invid += 1
                added.append((inv_id, inv))
            if added:
                self.logger.info(
                    "[APPLY] loop=%s added=%d",
                    loop_obj.display_name,
                    len(added),
                )
                for inv_id, inv in added:
                    self.logger.info("  + %s: %s", inv_id, inv)
            else:
                self.logger.info("[APPLY] loop=%s added=0", loop_obj.display_name)


    def revise_loop_invariant_inference(self, error_msg_key, error_msg_value):
        # ensure 失败
        if self.is_contract_goal_id(error_msg_key):
            target_loop = self.last_loop_in_function(self.contract_goal_function(error_msg_key))
            if target_loop is None:
                return
            if isinstance(error_msg_value, dict):
                error_target = error_msg_value.get("target", error_msg_key)
                wp_goal = error_msg_value.get("context", "")
            else:
                error_target = str(error_msg_value)
                wp_goal = ""
            failure_property = self.describe_failed_property(
                error_msg_key,
                error_msg_value,
                "contract",
            )
            self.logger.info(
                "[REVISE] kind=contract goal=%s target=%s property=%s",
                error_msg_key,
                self.compact_text(error_target, 180),
                failure_property,
            )
            self.diagnose_and_refine_failure(
                current_loop=target_loop,
                error_invariant=f"{error_msg_key}: {error_target}",
                wp_goal=wp_goal,
                failure_kind="contract",
                failure_property=failure_property,
            )
            return
        
        error_loop_invariant = self.search_invariant_content_by_id(error_msg_key)

        error_loop_index = self.search_invariant_by_id(error_msg_key)
        if error_loop_index is None:
            # print(f"Cannot locate invariant {error_msg_key}")
            return
        current_loop = self.loop_list[error_loop_index]
        # print(f"Revising invariant {error_msg_key} in Loop {error_loop_index}, type={error_msg_value}")

        # invariant 失败
        if error_msg_key.startswith("i"):
            failure_kind = error_msg_value.get("status", "unknown")
            failure_property = self.describe_failed_property(
                error_msg_key,
                error_msg_value,
                failure_kind,
            )
            self.logger.info(
                "[REVISE] kind=invariant id=%s loop=%s status=%s property=%s invariant=%s",
                error_msg_key,
                current_loop.display_name,
                failure_kind,
                failure_property,
                self.compact_text(error_loop_invariant, 180),
            )
            self.diagnose_and_refine_failure(
                current_loop=current_loop,
                error_invariant=f"{error_msg_key}: {error_loop_invariant}",
                wp_goal=error_msg_value.get("context", ""),
                failure_kind=failure_kind,
                failure_property=failure_property,
            )
            return

    def loop_annotation_matches(self, code: str):
        loop_pat = re.compile(
            r'(?P<indent>^[ \t]*)(?P<comment_full>/\*@(?P<comment>(?:(?!\*/).)*?)\*/)\s*'
            r'(?P<header>(?:for|while)\s*\(.*?\)\s*(?:\{)?)',
            re.S | re.M,
        )
        return [
            m for m in loop_pat.finditer(code)
            if re.search(r'\bloop\s+(assigns|variant|invariant)\b', m.group("comment"))
        ]

    def has_existing_loop_label(self, code: str, insert_pos: int) -> bool:
        prefix = code[:insert_pos]
        lines = prefix.splitlines()
        for line in reversed(lines):
            if not line.strip():
                continue
            return re.match(r"\s*//\s*Loop\s+[A-Za-z0-9_]+\s*$", line) is not None
        return False

    def inject_loop_index_into_code(self, code):
        """
        在整份文件中，每个带 ACSL loop 注释的循环前插入 Loop A/B/C/... 提示。
        缩进根据注释块内 loop assigns 的缩进确定。
        """
        matches = self.loop_annotation_matches(code)
        new_code = code
        offset = 0

        for idx, m in enumerate(matches):
            comment_block = m.group("comment")
            comment_start = m.start("comment_full")

            insert_pos = comment_start + offset
            if self.has_existing_loop_label(new_code, insert_pos):
                continue

            assign_indent_match = re.search(r"(\n[ \t]*)loop assigns", comment_block)
            if assign_indent_match:
                extra_indent = assign_indent_match.group(1)
                spaces = len(extra_indent.expandtabs(4)) - 1
                label_indent = " " * max(spaces - 4, 0)
            else:
                label_indent = m.group("indent")

            label_line = f"{label_indent}// Loop {loop_label(idx)}\n"
            new_code = new_code[:insert_pos] + label_line + new_code[insert_pos:]
            offset += len(label_line)

        return new_code

    def inject_reasoning_prompt_code(self, loop_index: int, hint: str = "[Hint] Please infer the loop invariant for the following loop.") -> str:
        return self.inject_reasoning_prompt_code_multi({loop_index: hint})

    def inject_reasoning_prompt_code_multi(self, loop_hints: dict) -> str:
        code = self.llm_prompt_code_base()
        matches = self.loop_annotation_matches(code)
        if not matches:
            raise ValueError(f"文件 {self.file_basename} 中未找到带注释的循环。")

        new_code = code
        offset = 0
        for loop_index, hint in sorted(loop_hints.items()):
            if loop_index < 0 or loop_index >= len(matches):
                raise IndexError(f"循环索引 {loop_index} 超出范围（共 {len(matches)} 个循环）。")

            m = matches[loop_index]
            cm_start = m.start("comment_full") + offset
            cm_end = m.end("comment_full") + offset
            original_comment_full = new_code[cm_start:cm_end]
            inner = original_comment_full[len("/*@"):-len("*/")]

            assign_indent_match = re.search(r'\n([ \t]*)loop assigns', inner)
            if assign_indent_match:
                indent = assign_indent_match.group(1)
            else:
                indent = "    "

            prompt_line = f"\n{indent}{hint}\n"
            insert_pos = cm_start + len("/*@")
            new_code = new_code[:insert_pos] + prompt_line + new_code[insert_pos:]
            offset += len(prompt_line)

        return new_code

    def inject_invariants(self) -> str:
        matches = self.loop_annotation_matches(self.code)
        if not matches:
            print(f"警告：文件 {self.file_basename} 中未找到带注释的循环。")

        new_code = self.code
        offset = 0

        for lp in self.loop_list:
            invariants: dict = lp.loop_invariant
            if not invariants:
                continue

            if lp.index >= len(matches):
                print(f"警告：找不到第 {lp.index + 1} 个循环对应的注释块，跳过注入。")
                continue

            m = matches[lp.index]
            cm_start = m.start("comment_full")
            cm_end = m.end("comment_full")

            original_comment_full = new_code[
                cm_start + offset : cm_end + offset
            ]
            inner = original_comment_full[len("/*@"):-len("*/")]

            assign_indent = "    "  
            assign_match = re.search(r'(^[ \t]*)loop assigns', inner, re.M)
            if assign_match:
                assign_indent = assign_match.group(1)  
            else:
                first_line_match = re.search(r'(^[ \t]*)', inner)
                if first_line_match:
                    assign_indent = first_line_match.group(1) + "    "

            inv_lines = "".join([
                f"{assign_indent}loop invariant {id}: {inv};\n\n"
                for id, inv in invariants.items()
            ])

            if 'loop assigns' in inner:
                def repl(m_assign):
                    indent = m_assign.group(1)
                    return f"{inv_lines}\n{indent}loop assigns"

                new_inner = re.sub(
                    r'(^[ \t]*)loop assigns',
                    repl,
                    inner,
                    count=1,
                    flags=re.M,
                )
            else:
                new_inner = inner.rstrip() + inv_lines + "\n"

            new_comment_full = "/*@" + new_inner + "*/"

            new_code = (
                new_code[: cm_start + offset]
                + new_comment_full
                + new_code[cm_end + offset :]
            )
            offset += len(new_comment_full) - (cm_end - cm_start)

        return new_code

def clean_lines_block(lines):
    filtered = []
    in_comment = False
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if not in_comment and '/*' in s:
            if '*/' in s:
                continue
            in_comment = True
            continue
        if in_comment:
            if '*/' in s:
                in_comment = False
            continue
        if s in ('{', '}', '};'):
            continue
        filtered.append(s)
    return " ".join(filtered).strip()


def preserve_acsl_assert_blocks(text: str) -> str:
    def repl(match):
        comment = match.group("comment")
        assertions = []
        for m in re.finditer(r"\bassert\b", comment):
            clause, _ = scan_clause_from(comment, m.end())
            if clause:
                body = re.sub(r"\s+", " ", clause).strip()
                assertions.append(f"//@ assert {body};")
        return "\n".join(assertions)

    return re.sub(r"/\*@(?P<comment>.*?)\*/", repl, text, flags=re.DOTALL)


def clean_code_snippet(text: str) -> str:
    text = preserve_acsl_assert_blocks(text)
    return clean_lines_block(text.splitlines())


def join_statements(*parts: str) -> str:
    return " ".join(part.strip() for part in parts if part and part.strip()).strip()


def as_statement(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    return text if text.endswith(";") else text + ";"


def parse_loop_header(header: str):
    stripped = header.strip()
    if stripped.startswith("for"):
        m = re.search(r'for\s*\((.*?)\)', header, re.S)
        if not m:
            return "for", "", "", ""
        parts = [p.strip() for p in m.group(1).split(';')]
        while len(parts) < 3:
            parts.append("")
        return "for", as_statement(parts[0]), parts[1], as_statement(parts[2])

    m = re.search(r'\((.*?)\)', header, re.S)
    return "while", "", m.group(1).strip() if m else "", ""


def skip_masked_whitespace(masked_code: str, pos: int) -> int:
    while pos < len(masked_code) and masked_code[pos].isspace():
        pos += 1
    return pos


def find_unbraced_statement_end(masked_code: str, stmt_start: int) -> int:
    """Return the exclusive end offset of a single C statement body."""
    i = skip_masked_whitespace(masked_code, stmt_start)
    n = len(masked_code)
    if i >= n:
        return n

    if masked_code[i] == "{":
        close_pos = find_matching_brace(masked_code, i)
        return (close_pos + 1) if close_pos is not None else n

    paren = 0
    brack = 0
    brace = 0
    while i < n:
        ch = masked_code[i]
        if ch == "(":
            paren += 1
        elif ch == ")":
            paren = max(paren - 1, 0)
        elif ch == "[":
            brack += 1
        elif ch == "]":
            brack = max(brack - 1, 0)
        elif ch == "{":
            brace += 1
        elif ch == "}":
            if brace == 0:
                return i
            brace -= 1
        elif ch == ";" and paren == 0 and brack == 0 and brace == 0:
            return i + 1
        i += 1

    return n


def detect_loops_and_positions(body: str):
    loop_pat = re.compile(
        r'/\*@(?P<comment>(?:(?!\*/).)*?)\*/\s*(?P<keyword>for|while)\s*\(',
        re.S,
    )
    matches = [
        m for m in loop_pat.finditer(body)
        if re.search(r'\bloop\s+(assigns|variant|invariant)\b', m.group("comment"))
    ]
    lines = body.splitlines()
    masked_body = mask_comments_and_strings(body)
    loop_infos = []
    for idx, m in enumerate(matches):
        header_start = m.start("keyword")
        open_paren = masked_body.find("(", header_start)
        close_paren = find_matching_paren(masked_body, open_paren) if open_paren != -1 else None
        if close_paren is None:
            continue

        after_header = skip_masked_whitespace(masked_body, close_paren + 1)
        if after_header < len(masked_body) and masked_body[after_header] == "{":
            open_brace = after_header
            end_pos = find_matching_brace(masked_body, open_brace)
            if end_pos is None:
                end_pos = len(body) - 1
            header = body[header_start:open_brace + 1]
            body_start_pos = open_brace
            body_content_start_pos = open_brace + 1
            body_content_end_pos = end_pos
        else:
            stmt_start = after_header
            stmt_end = find_unbraced_statement_end(masked_body, stmt_start)
            header = body[header_start:close_paren + 1]
            body_start_pos = header_start
            body_content_start_pos = stmt_start
            body_content_end_pos = stmt_end
            end_pos = max(stmt_end - 1, close_paren)

        start_line = body[:header_start].count('\n') + 1
        end_line = body[:end_pos].count('\n') + 1
        loop_infos.append({
            'index': idx,
            'comment': m.group('comment'),
            'header': header,
            'start': start_line,
            'end': end_line,
            'body_start_pos': body_start_pos,
            'body_end_pos': end_pos,
            'body_content_start_pos': body_content_start_pos,
            'body_content_end_pos': body_content_end_pos,
            'comment_start_pos': m.start(),
            'header_start_pos': header_start,
        })
    return loop_infos, lines

def build_loops(code: str, filename: str, model: str, logger: logging.Logger, local_model_context=None, ablation_mode: bool = False) -> LoopList:
    function_regions = find_function_regions(code)
    if not function_regions:
        raise ValueError(f"文件 {filename} 中未找到可解析的 C 函数定义。")

    loops = []
    global_index = 0

    for function_region in function_regions:
        func_name = function_region["name"]
        body = code[function_region["body_start"]:function_region["body_end"]]
        loop_infos, lines = detect_loops_and_positions(body)
        if not loop_infos:
            continue

        local_to_global = {
            info["index"]: global_index + local_idx
            for local_idx, info in enumerate(loop_infos)
        }

        parent_local = {}
        for j, child in enumerate(loop_infos):
            containers = []
            for i, candidate_parent in enumerate(loop_infos):
                if i == j:
                    continue
                if (
                    candidate_parent["body_start_pos"] < child["comment_start_pos"]
                    and child["body_end_pos"] < candidate_parent["body_end_pos"]
                ):
                    containers.append(i)
            if containers:
                parent_local[j] = min(
                    containers,
                    key=lambda idx: loop_infos[idx]["body_end_pos"] - loop_infos[idx]["body_start_pos"]
                )

        direct_children = {i: [] for i in range(len(loop_infos))}
        for child_idx, parent_idx in parent_local.items():
            direct_children[parent_idx].append(child_idx)
        for children in direct_children.values():
            children.sort(key=lambda idx: loop_infos[idx]["comment_start_pos"])

        siblings_by_parent = {}
        for i in range(len(loop_infos)):
            siblings_by_parent.setdefault(parent_local.get(i), []).append(i)
        for siblings in siblings_by_parent.values():
            siblings.sort(key=lambda idx: loop_infos[idx]["comment_start_pos"])

        for i, info in enumerate(loop_infos):
            header = info['header']
            loop_type, init_state, cond, update_state = parse_loop_header(header)

            parent_idx = parent_local.get(i)
            siblings = siblings_by_parent[parent_idx]
            idx_in_siblings = siblings.index(i)
            if idx_in_siblings == 0:
                if parent_idx is None:
                    pre_start = 0
                else:
                    pre_start = loop_infos[parent_idx]["body_content_start_pos"]
            else:
                prev_sibling = loop_infos[siblings[idx_in_siblings - 1]]
                pre_start = prev_sibling["body_end_pos"] + 1
            pre_end = info["comment_start_pos"]
            pre_state = join_statements(clean_code_snippet(body[pre_start:pre_end]), init_state)

            if idx_in_siblings < len(siblings) - 1:
                next_sibling = loop_infos[siblings[idx_in_siblings + 1]]
                post_end = next_sibling["comment_start_pos"]
            elif parent_idx is None:
                post_end = len(body)
            else:
                post_end = loop_infos[parent_idx]["body_end_pos"]
            post_start = info["body_end_pos"] + 1
            post_state = clean_code_snippet(body[post_start:post_end])

            loop_body = clean_code_snippet(body[info["body_content_start_pos"]:info["body_content_end_pos"]])

            assigns = re.findall(r'loop assigns (.*?);', info['comment'])

            if direct_children[i]:
                loop_state = [local_to_global[loop_infos[j]['index']] for j in direct_children[i]]
            else:
                loop_state = join_statements(loop_body, update_state)

            abs_body_start = function_region["body_start"] + info["body_start_pos"]
            abs_body_end = function_region["body_start"] + info["body_end_pos"]
            abs_body_content_start = function_region["body_start"] + info["body_content_start_pos"]
            abs_body_content_end = function_region["body_start"] + info["body_content_end_pos"]
            abs_comment_start = function_region["body_start"] + info["comment_start_pos"]

            loop_obj = Loop(
                index=local_to_global[info['index']],
                function_name=func_name,
                loop_type=loop_type,
                pre_state=pre_state,
                condition=cond,
                loop_state=loop_state,
                post_state=post_state,
                loop_assigns=assigns,
                loop_invariant={},
                init_state=init_state,
                update_state=update_state,
                body_start_pos=abs_body_start,
                body_end_pos=abs_body_end,
                body_content_start_pos=abs_body_content_start,
                body_content_end_pos=abs_body_content_end,
                comment_start_pos=abs_comment_start,
                line_start=code[:abs_body_start].count("\n") + 1,
                line_end=code[:abs_body_end].count("\n") + 1
            )
            loops.append(loop_obj)

        global_index += len(loop_infos)

    loop_list = LoopList(
        code,
        filename,
        model,
        logger,
        function_regions=function_regions,
        local_model_context=local_model_context,
        ablation_mode=ablation_mode,
    )

    for lp in loops:
        loop_list.add_loop(lp)

    loop_list.loop_graph_construct()
    loop_list.build_transition_edges()
    loop_list.build_call_edges()
    return loop_list

if __name__ == "__main__":
    with open("../Benchmark/OOPSLA/oopsla_17.c", "r", encoding="utf-8") as f:
        c = f.read()

    loop_list: LoopList = build_loops(c, 'oopsla_17', 'deepseek/deepseek-chat-v3-0324', None)


    print("=== Output ===\n")
    for lp in loop_list.loop_list:
        print(f"Loop #{lp.index} ({lp.loop_type})")
        print(f"  pre_state: {lp.pre_state}")
        print(f"  condition: {lp.condition}")
        print(f"  loop_state: {lp.loop_state}")
        print(f"  post_state: {lp.post_state}")
        print(f"  loop_assigns: {lp.loop_assigns}") 
        print(f"  loop_invariant: {lp.loop_invariant}")
        print(f"  pre_loop: {lp.pre_loop}")
        print(f"  post_loop: {lp.post_loop}\n")
    
    print(loop_list.ensures)
