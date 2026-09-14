import copy, string

class Loop:
    def __init__(self, index: int,
                 loop_type: str,
                 pre_state: str,
                 condition: str,
                 loop_state,
                 post_state: str,
                 loop_assigns: list,
                 loop_invariant: dict,
                 function_name: str = "",
                 init_state: str = "",
                 update_state: str = "",
                 body_start_pos=None,
                 body_end_pos=None,
                 body_content_start_pos=None,
                 body_content_end_pos=None,
                 comment_start_pos=None,
                 line_start=None,
                 line_end=None) -> None:
        self.index = index
        self.alphabet = string.ascii_uppercase[index] if self.index < len(string.ascii_uppercase) else f"Z{self.index - 25}"
        self.function_name = function_name
        self.loop_type = loop_type
        self.pre_state = pre_state
        self.condition = condition
        self.loop_state = loop_state
        self.post_state = post_state
        self.loop_assigns = copy.deepcopy(loop_assigns)
        self.loop_invariant = copy.deepcopy(loop_invariant)
        self.init_state = init_state
        self.update_state = update_state
        self.pre_loop = None
        self.post_loop = []
        self.body_start_pos = body_start_pos
        self.body_end_pos = body_end_pos
        self.body_content_start_pos = body_content_start_pos
        self.body_content_end_pos = body_content_end_pos
        self.comment_start_pos = comment_start_pos
        self.line_start = line_start
        self.line_end = line_end

    def __repr__(self):
        scope = f" {self.function_name}" if self.function_name else ""
        return f"<Loop {self.index}{scope} {self.loop_type}>"

    @property
    def display_name(self):
        if self.function_name:
            return f"{self.alphabet}@{self.function_name}"
        return self.alphabet
