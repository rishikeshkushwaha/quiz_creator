import re
import random
from io import BytesIO

import pandas as pd
import streamlit as st
from pypdf import PdfReader

# -----------------------------------------------------------------------------
# CONSTANTS
# -----------------------------------------------------------------------------
QUEST_PDF = "pdf/question.pdf"
ANSWER_PDF = "pdf/answer.pdf"
SOLVED_PDF = "pdf/UPPSC_Preview_General_Studies_Solved_Paper_2026.pdf"

TOTAL_FULL = 150
TOTAL_MARKS = 200
MARKS_CORRECT = TOTAL_MARKS / TOTAL_FULL          # 1.3333... per correct
MARKS_WRONG = MARKS_CORRECT / 3                   # 0.4444... per wrong

PAGE_TITLE = "UP-PCS Prelims 2026 Practice Hub"

# Modes of the exam
MODE_TEST = "Test Mode"
MODE_PRACTICE = "Practice Mode"


def score_params(questions):
    """Return (num_questions, marks_per_correct, marks_per_wrong, total_marks).

    Marking is fixed per question: +1.33 correct, −0.44 wrong, 0 for skipped.
    Total marks = number of questions × 1.33 (e.g. 150 → 200 marks).
    """
    n = len(questions) or 1
    mc = MARKS_CORRECT      # 1.3333...
    mw = MARKS_WRONG        # 0.4444...
    total = round(n * MARKS_CORRECT)
    return n, mc, mw, total


# -----------------------------------------------------------------------------
# TEXT CLEANING (fix PDF font artifacts from extraction)
# -----------------------------------------------------------------------------
CHAR_REPLACEMENTS = {
    "\u00e6": "'", "\u00c6": "'",   # æ/Æ -> apostrophe
    "\u00fb": "-", "\u00db": "-",   # û/Û -> en dash
}


def clean_text(s, collapse_newlines=False):
    """Clean garbled characters produced by the PDF's custom font mapping."""
    s = re.sub(r"Adju\s*ment", "Adjustment", s)
    s = re.sub(r"(?<!s)atement(s)?\b", r"statement\1", s)
    s = re.sub(r"(?<!s)tartup\b", "startup", s)
    s = re.sub(r"\bV\s+oters", "Voters", s)
    s = re.sub(r"Augu\s+", "August", s)
    for k, v in CHAR_REPLACEMENTS.items():
        s = s.replace(k, v)
    if collapse_newlines:
        s = re.sub(r"\s+", " ", s)
    else:
        s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


# -----------------------------------------------------------------------------
# PDF PARSING (deterministic, no LLM)
# -----------------------------------------------------------------------------
def is_hindi(line):
    """True if the line contains Devanagari (Hindi) characters."""
    return bool(re.search(r"[\u0900-\u097F]", line))


def is_noise(line):
    """True for boilerplate / footer / page-number lines we can drop."""
    s = line.strip().lower()
    if re.search(r"click here for more test series", s):
        return True
    if "copyright" in s:
        return True
    if re.search(r"phone\s*:\s*\d", s):
        return True
    if re.search(r"e-?mail", s):
        return True
    if "website:" in s:
        return True
    if re.search(r"^\d{1,2}$", s):
        return True
    if "scan for feedback" in s:
        return True
    if s.startswith("sectional te"):
        return True
    return False


def _extract_question_lines(stream=None):
    """Extract clean English-only lines from the question booklet."""
    reader = PdfReader(stream) if stream else PdfReader(QUEST_PDF)
    lines = []
    for page in reader.pages:
        text = page.extract_text() or ""
        for line in text.split("\n"):
            if is_hindi(line) or is_noise(line):
                continue
            lines.append(line)
    return lines


def _find_blocks(lines):
    """Locate each question block via its leading number + 4 option markers."""
    blocks = []
    i, n = 0, len(lines)
    while i < n:
        m = re.match(r"^\s*(\d{1,3})\.\s+(.*)$", lines[i])
        if m:
            num = int(m.group(1))
            rest = m.group(2)
            if re.match(r"^[A-Za-z'\u2018\u2019\"\u201C\u201D]", rest):
                j, count = i, 0
                while j < n:
                    count += len(re.findall(r"\([a-d]\)", lines[j]))
                    if count >= 4:
                        break
                    j += 1
                if count >= 4:
                    blocks.append((num, i, j))
                    i = j + 1
                    continue
        i += 1
    return blocks


def _parse_block(num, lines):
    """Split a question block into question text + 4 options (a)-(d)."""
    text = "\n".join(lines)
    matches = list(re.finditer(r"\(([a-d])\)", text))
    if len(matches) < 4:
        return None
    options = {}
    for i, m in enumerate(matches[:4]):
        letter = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i < 3 else len(text)
        options[letter] = clean_text(text[start:end], collapse_newlines=True)
    qtext = text[:matches[0].start()].strip()
    qtext = re.sub(r"^\s*\d{1,3}\.\s*", "", qtext)
    qtext = re.sub(
        r"\s*Select\s+the\s+correct\s+answer.*$", "",
        qtext, flags=re.IGNORECASE | re.DOTALL
    )
    qtext = re.sub(r"\s*Code:\s*$", "", qtext, flags=re.IGNORECASE)
    qtext = clean_text(qtext, collapse_newlines=False)
    return {"num": num, "question": qtext, "options": options}


def _extract_answer_key(stream=None):
    """Read the official answer key (question number -> letter)."""
    reader = PdfReader(stream) if stream else PdfReader(ANSWER_PDF)
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    pairs = re.findall(r"(\d{1,3})\s*\(([a-d])\)", text)
    return {int(n): let.lower() for n, let in pairs}


def build_quiz(q_stream=None, a_stream=None):
    """Parse the PDFs into a list of question dicts (order preserved)."""
    lines = _extract_question_lines(q_stream)
    blocks = _find_blocks(lines)
    key = _extract_answer_key(a_stream)
    questions = []
    for num, start, end in blocks:
        block = _parse_block(num, lines[start:end + 1])
        if block and num in key:
            block["answer"] = key[num]
            questions.append(block)
    return questions


@st.cache_data(show_spinner=False)
def load_quiz(q_bytes: bytes = None, a_bytes: bytes = None):
    """Load the quiz from bundled PDFs or uploaded PDF byte streams."""
    return build_quiz(
        BytesIO(q_bytes) if q_bytes else None,
        BytesIO(a_bytes) if a_bytes else None,
    )


# -----------------------------------------------------------------------------
# SOLVED-PAPER PARSING (UPPSC Preview: question -> answer -> explanation)
#
# Format per block:
#   <num>.  <question text possibly spanning lines>
#     (a) option  (b) option
#     (c) option  (d) option
#     [optional source line e.g. "U.P.P.C.S. (Mains) 2010"]
#     Ans. (x)
#     <explanation text until the next "<num>." block>
#
# `*`-prefixed one-liner sub-questions (no options / no Ans.) are skipped.
# -----------------------------------------------------------------------------
def _is_source_line(line):
    """Heuristic: a line that looks like an exam source citation."""
    s = line.strip()
    if re.search(r"\bU\.?P\.?\s*P\.?C\.?S\b", s, re.IGNORECASE):
        return True
    if re.search(r"\bU\.?D\.?A\.?/L\.?D\.?A\.?\b", s, re.IGNORECASE):
        return True
    if re.search(r"\bR\.?O\.?/A\.?R\.?O\.?\.?\b", s, re.IGNORECASE):
        return True
    if re.search(r"\b(Pre|Mains|Prelims)?\s*\(?\b\d{4}\b\)?", s):
        if re.search(r"Exam|P\.?C\.?S|Paper|GIC|B\.?E\.?O|U\.?D\.?A|R\.?O\.?/A\.?R\.?O", s, re.IGNORECASE):
            return True
    return False


def _is_one_liner(line):
    """True for `*`-prefixed one-liner sub-questions (no options/answer)."""
    return bool(re.match(r"^\s*\*\s+", line))


def parse_solved_lines(lines):
    """Parse a list of page lines into question dicts.

    Each returned dict has:
      num, question, options {a..d}, answer (letter), explanation (str)
    """
    text = "\n".join(lines)
    # Split into blocks starting at a numbered question.
    # A block runs from "N." up to (but not including) the next "M." where M is
    # a fresh question number at the start of a line.
    pattern = re.compile(r"(?m)^(\d{1,3})\.\s+(?=[A-Z\"\u2018\u201c])")
    matches = list(pattern.finditer(text))
    questions = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end]
        q = _parse_solved_block(m.group(1), block)
        if q:
            questions.append(q)
    return questions


def _parse_solved_block(num, block):
    """Parse a single Q->Ans->Explanation block."""
    lines = block.split("\n")
    # Find the Ans. line
    ans_idx = None
    ans_letter = None
    for i, ln in enumerate(lines):
        am = re.search(r"Ans\.?\s*[:\-]?\s*\(?([a-d])\)?", ln, re.IGNORECASE)
        if am:
            ans_idx = i
            ans_letter = am.group(1).lower()
            break
    if ans_idx is None:
        return None

    # Explanation = everything after the Ans. line
    explanation = clean_text(
        "\n".join(lines[ans_idx + 1:]), collapse_newlines=True
    )

    # Question + options = everything before the Ans. line
    head = "\n".join(lines[:ans_idx])

    # Extract the 4 option markers
    opt_matches = list(re.finditer(r"\(([a-d])\)", head))
    if len(opt_matches) < 4:
        return None

    options = {}
    for i, om in enumerate(opt_matches[:4]):
        letter = om.group(1)
        ostart = om.end()
        oend = opt_matches[i + 1].start() if i < 3 else len(head)
        options[letter] = clean_text(head[ostart:oend], collapse_newlines=True)

    # Question text = before the first option marker, minus the leading number
    qtext = head[:opt_matches[0].start()].strip()
    qtext = re.sub(r"^\s*\d{1,3}\.\s*", "", qtext)
    # Drop trailing source-citation line(s) from the question text
    qlines = [ln for ln in qtext.split("\n") if not _is_source_line(ln)]
    qtext = clean_text("\n".join(qlines), collapse_newlines=False)
    if not qtext:
        return None

    return {
        "num": int(num),
        "question": qtext,
        "options": options,
        "answer": ans_letter,
        "explanation": explanation,
    }


def extract_pages(reader, start_page, end_page):
    """Return the raw extracted text for pages [start_page, end_page] (1-based)."""
    lines = []
    n = len(reader.pages)
    s = max(1, min(int(start_page), n))
    e = max(s, min(int(end_page), n))
    for p in range(s - 1, e):
        text = reader.pages[p].extract_text() or ""
        lines.append(text)
    # Drop one-liners and obvious header/footer noise, keep the rest.
    keep = []
    for ln in "\n".join(lines).split("\n"):
        if _is_one_liner(ln):
            continue
        keep.append(ln)
    return keep


@st.cache_data(show_spinner=False)
def load_solved(start_page, end_page):
    """Parse the solved-paper PDF over the given inclusive page range."""
    reader = PdfReader(SOLVED_PDF)
    lines = extract_pages(reader, start_page, end_page)
    return parse_solved_lines(lines)


# -----------------------------------------------------------------------------
# SESSION STATE HELPERS
# -----------------------------------------------------------------------------
def init_state():
    defaults = {
        "questions": None,   # full parsed question list
        "order": None,       # selected order of indices
        "idx": 0,
        "started": False,
        "selected": None,
        "responses": {},     # position -> {question, user, correct, status}
        "nav_jump": 0,       # keeps the "Jump to question" box in sync
        "pdf_source": "Bundled UP-PCS Prelims 2026 PDFs",
        "is_solved": False,  # True when the active set came from Solved-Paper PDF
        "mode": MODE_TEST,   # MODE_TEST or MODE_PRACTICE
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def start_test(num_q, shuffle, mode):
    questions = st.session_state.questions
    order = list(range(len(questions)))
    if shuffle:
        random.shuffle(order)
    num_q = min(num_q, len(questions))
    st.session_state.order = order[:num_q]
    st.session_state.idx = 0
    st.session_state.selected = None
    st.session_state.responses = {}
    st.session_state.nav_jump = 0
    st.session_state.mode = mode
    st.session_state.started = True


# -----------------------------------------------------------------------------
# NAVIGATION CALLBACKS
#
# Streamlit runs on_click/on_change callbacks BEFORE the script body executes,
# so they are the ONLY safe place to write to a widget key (e.g. `nav_jump`)
# that is also rendered later in the script.
# -----------------------------------------------------------------------------
def _go(i):
    """on_click handler for Previous / Next / Skip buttons.

    Runs before any widget is instantiated, so updating `nav_jump` is safe.
    """
    st.session_state.idx = i
    st.session_state.selected = None
    st.session_state.nav_jump = max(0, i)


def _nav_change():
    """on_change handler for the Jump-to-question selectbox."""
    st.session_state.idx = st.session_state.nav_jump
    st.session_state.selected = None


def reset_app():
    for k in list(st.session_state.keys()):
        del st.session_state[k]
    st.rerun()


# -----------------------------------------------------------------------------
# UI: SIDEBAR
# -----------------------------------------------------------------------------
def render_sidebar():
    n_total = len(st.session_state.questions)
    with st.sidebar:
        st.header("⚙️ Test Settings")

        with st.expander("📤 Upload Your Own PDFs", expanded=False):
            st.caption(
                "Same format as the bundled set: a question booklet PDF plus a "
                "separate answer-key PDF (`1 (d) 2 (a) ...`)."
            )
            q_file = st.file_uploader("Question paper PDF", type=["pdf"], key="up_q")
            a_file = st.file_uploader("Answer key PDF", type=["pdf"], key="up_a")
            can_load = q_file is not None and a_file is not None
            if st.button("📂 Load PDFs", use_container_width=True, disabled=not can_load):
                qs = load_quiz(q_bytes=q_file.getvalue(), a_bytes=a_file.getvalue())
                if qs:
                    st.session_state.questions = qs
                    st.session_state.pdf_source = f"{q_file.name} + {a_file.name}"
                    st.session_state.is_solved = False
                    st.session_state.order = None
                    st.session_state.idx = 0
                    st.session_state.started = False
                    st.session_state.selected = None
                    st.session_state.responses = {}
                    st.session_state.nav_jump = 0
                    st.session_state.pop("slider_num_q", None)
                    st.rerun()
                else:
                    st.error("Could not parse the uploaded PDFs. Please check the format.")

        with st.expander("📖 Solved-Paper PDF (Q→Answer→Expl)", expanded=False):
            st.caption(
                "Parse the UPPSC Preview solved paper by page range. Each question "
                "shows its answer + explanation after you submit."
            )
            st.number_input(
                "Start page", min_value=1, max_value=1160, value=4, step=1, key="sp_start"
            )
            st.number_input(
                "End page", min_value=1, max_value=1160, value=30, step=1, key="sp_end"
            )
            if st.button("📖 Parse Pages", use_container_width=True):
                s, e = int(st.session_state.sp_start), int(st.session_state.sp_end)
                if s > e:
                    st.error("Start page must be ≤ end page.")
                else:
                    with st.spinner(f"Parsing pages {s}–{e} of the solved paper..."):
                        qs = load_solved(s, e)
                    if qs:
                        st.session_state.questions = qs
                        st.session_state.is_solved = True
                        st.session_state.pdf_source = (
                            f"Solved-Paper PDF (pages {s}–{e})"
                        )
                        st.session_state.order = None
                        st.session_state.idx = 0
                        st.session_state.started = False
                        st.session_state.selected = None
                        st.session_state.responses = {}
                        st.session_state.nav_jump = 0
                        st.session_state.pop("slider_num_q", None)
                        st.rerun()
                    else:
                        st.error(
                            "No complete questions found in that page range. Try a "
                            "range that includes question pages (e.g. 4–30)."
                        )

        st.caption("**Mode**")
        st.radio(
            "Choose exam mode:",
            options=[MODE_TEST, MODE_PRACTICE],
            key="mode_choice",
            help=(
                f"**{MODE_TEST}:** answers are revealed only after the test is complete. "
                f"**{MODE_PRACTICE}:** answers are revealed immediately after each submission."
            ),
        )
        st.divider()

        st.caption("**Marking Scheme** (UP-PCS Prelims)")
        st.caption(f"✅ **+{MARKS_CORRECT:.3f}** per correct")
        st.caption(f"❌ **−{MARKS_WRONG:.4f}** per incorrect")
        st.caption(f"Total: **{round(n_total * MARKS_CORRECT)} marks** / **{n_total} questions**")
        st.caption("0 marks for skipped / unattempted")
        st.divider()

        step = 5 if n_total >= 10 else 1
        st.slider(
            "Number of questions",
            min_value=min(10, n_total),
            max_value=n_total,
            value=n_total,
            step=step,
            key="slider_num_q",
        )
        st.checkbox("Shuffle question order", value=False, key="chk_shuffle")
        st.divider()
        if st.button("🔄 Reset / New Test", use_container_width=True):
            reset_app()


# -----------------------------------------------------------------------------
# UI: INTRO SCREEN
# -----------------------------------------------------------------------------
def render_intro():
    st.title(f"📚 {PAGE_TITLE}")
    st.subheader("Practice Test Series – 2026 · General Studies-I (English Version)")

    n_total = len(st.session_state.questions)
    src = st.session_state.pdf_source

    mode_desc = (
        "✅ Answers are revealed instantly after each submission."
        if st.session_state.mode_choice == MODE_PRACTICE
        else "🔒 Answers are revealed only after the full test is submitted."
    )

    st.markdown(
        f"""
        **Active paper:** `{src}`
        **Selected mode:** `{st.session_state.mode_choice}` — {mode_desc}

        This app parses the question booklet and answer key from PDFs — running
        **100% offline** with no LLM required.

        - **{n_total} questions** extracted (English only)
        - **Marking:** +{MARKS_CORRECT:.3f} for correct, −{MARKS_WRONG:.4f} for incorrect, 0 for skipped
        - **Max score:** {round(n_total * MARKS_CORRECT)} marks
        - Jump to **any question** anytime (Previous / Next / dropdown)
        - Upload your own question + answer PDFs from the sidebar
        - Full answer review table + **CSV export** at the end
        """
    )

    st.divider()
    st.info("Configure the number of questions, mode & shuffling in the **sidebar**, then press Start.")
    if st.button("🚀 Start Test", type="primary", use_container_width=True):
        start_test(
            num_q=st.session_state.slider_num_q,
            shuffle=st.session_state.chk_shuffle,
            mode=st.session_state.mode_choice,
        )
        st.rerun()


# -----------------------------------------------------------------------------
# UI: QUIZ SCREEN
# -----------------------------------------------------------------------------
def render_quiz():
    questions = st.session_state.questions
    order = st.session_state.order
    total = len(order)
    idx = st.session_state.idx

    if idx < total:
        _render_question(questions, order, idx, total)
    else:
        _render_results(questions, order, total)


def _render_nav(idx, total, responses):
    """Previous / Jump-to / Next controls.

    All navigation uses on_click / on_change callbacks so that updating the
    `nav_jump` widget key never triggers the StreamlitAPIException.
    """
    nav1, nav2, nav3 = st.columns([1, 2, 1])
    with nav1:
        st.button(
            "⬅️ Previous",
            use_container_width=True,
            disabled=(idx == 0),
            on_click=_go,
            args=(idx - 1,),
        )
    with nav2:
        st.selectbox(
            "Jump to question",
            options=list(range(total)),
            index=min(st.session_state.nav_jump, total - 1),
            format_func=lambda i: f"Q{i + 1}" + (" ✅" if i in responses else ""),
            key="nav_jump",
            on_change=_nav_change,
        )
    with nav3:
        st.button(
            "⏭️ Next",
            use_container_width=True,
            disabled=(idx >= total - 1),
            on_click=_go,
            args=(idx + 1,),
        )


def _render_question(questions, order, idx, total):
    q = questions[order[idx]]
    responses = st.session_state.responses
    submitted = idx in responses
    saved = responses.get(idx)

    _, mc, mw, tm = score_params(questions)

    answered = len(responses)
    correct = sum(1 for r in responses.values() if r["status"] == "Correct")
    wrong = sum(1 for r in responses.values() if r["status"] == "Wrong")
    marks = max(0.0, correct * mc - wrong * mw)

    st.progress(idx / total, text=f"Question {idx + 1} of {total}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Attempted", f"{answered}/{total}")
    c2.metric("Correct", correct)
    c3.metric("Wrong", wrong)
    c4.metric("Score", f"{marks:.2f}")
    st.divider()

    _render_nav(idx, total, responses)
    st.divider()

    st.markdown(
        f"<div style='white-space: pre-line; font-size:1.05rem;'>"
        f"<b>Q{idx + 1}.</b> {q['question']}</div>",
        unsafe_allow_html=True,
    )

    opt_keys = [k for k in "abcd" if k in q["options"]]
    labels = {k: f"({k}) {q['options'][k]}" for k in opt_keys}

    default = saved["user"] if (saved and saved["user"] in opt_keys) else None

    def on_change():
        st.session_state.selected = st.session_state[f"radio_{idx}"]

    st.radio(
        "Choose the correct option:",
        options=opt_keys,
        format_func=lambda k: labels[k],
        key=f"radio_{idx}",
        index=opt_keys.index(default) if default else None,
        disabled=submitted,
        on_change=on_change,
    )

    if not submitted:
        colA, colB, _ = st.columns([1, 1, 4])
        with colA:
            if st.button("✅ Submit Answer", use_container_width=True, type="primary"):
                sel = st.session_state.selected
                if sel is None:
                    st.warning("Please select an option first.")
                else:
                    responses[idx] = {
                        "question": q["question"],
                        "user": sel,
                        "correct": q["answer"],
                        "status": "Correct" if sel == q["answer"] else "Wrong",
                    }
                    st.rerun()
        with colB:
            st.button(
                "⏭️ Skip Question",
                use_container_width=True,
                on_click=_go,
                args=(idx + 1,),
            )
    else:
        sel = saved["user"]
        correct_letter = q["answer"]
        mode = st.session_state.mode

        if mode == MODE_PRACTICE:
            # Practice Mode: reveal correctness + answer now.
            if sel == correct_letter:
                st.success(
                    f"✅ **Correct!** Answer: ({correct_letter.upper()}) {q['options'][correct_letter]}"
                )
            else:
                st.error(
                    f"❌ **Incorrect.** Your answer: ({sel.upper()}) {q['options'][sel]}. "
                    f"Correct answer: ({correct_letter.upper()}) {q['options'][correct_letter]}"
                )
            # Show explanation if available (solved-paper mode)
            explanation = q.get("explanation")
            if explanation:
                st.markdown(
                    f"<div style='background-color:#f0f4f8;border-left:5px solid #1f77b4;"
                    f"padding:12px;border-radius:4px;margin-top:8px;color:#102a43;'>"
                    f"<strong>📝 Explanation:</strong><br/>{explanation}</div>",
                    unsafe_allow_html=True,
                )
        else:
            # Test Mode: DO NOT reveal the answer. Only confirm the answer was saved.
            st.info(
                f"📝 Answer saved. You selected **({sel.upper()})**. "
                "The correct answer will be shown after the test is complete."
            )
        colA, _ = st.columns([1, 5])
        with colA:
            label = "🏁 View Results" if idx == total - 1 else "⏭️ Next Question"
            st.button(
                label,
                use_container_width=True,
                type="primary",
                on_click=_go,
                args=(idx + 1,),
            )


# -----------------------------------------------------------------------------
# UI: RESULTS SCREEN
# -----------------------------------------------------------------------------
def _render_results(questions, order, total):
    st.balloons()
    st.title("🏁 Test Complete")

    responses = st.session_state.responses
    correct = sum(1 for r in responses.values() if r["status"] == "Correct")
    wrong = sum(1 for r in responses.values() if r["status"] == "Wrong")
    skipped = total - len(responses)
    attempted = correct + wrong
    _, mc, mw, tm = score_params(questions)
    marks = max(0.0, correct * mc - wrong * mw)
    accuracy = (correct / attempted * 100) if attempted > 0 else 0.0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Score", f"{marks:.2f} / {tm}")
    c2.metric("Correct", correct)
    c3.metric("Wrong", wrong)
    c4.metric("Skipped", skipped)
    st.write(f"**Attempted:** {attempted}/{total}  ·  **Accuracy:** {accuracy:.1f}%")

    if accuracy >= 80:
        st.success("Excellent performance! 🎯")
    elif accuracy >= 60:
        st.info("Good performance 👍")
    elif accuracy >= 40:
        st.warning("Average performance — keep practising ⚠️")
    else:
        st.error("Needs more practice ❗")

    st.divider()
    st.subheader("🔍 Jump Back Into The Test")
    jump = st.selectbox(
        "Select a question to review / re-open:",
        options=list(range(total)),
        format_func=lambda i: f"Q{i + 1}" + (" ✅" if i in responses else "  — not answered"),
        key="review_jump",
    )

    def _open_review():
        st.session_state.idx = st.session_state.review_jump
        st.session_state.selected = None
        st.session_state.nav_jump = st.session_state.review_jump

    st.button("📖 Open Selected Question", use_container_width=True, on_click=_open_review)

    st.divider()
    st.subheader("📋 Detailed Review")

    rows = []
    for idx in range(total):
        q = questions[order[idx]]
        r = responses.get(idx)
        if r is None:
            rows.append({
                "Q#": q["num"],
                "Question": q["question"],
                "Your Answer": "—",
                "Correct Answer": f"({q['answer'].upper()})",
                "Status": "Skipped",
            })
        else:
            rows.append({
                "Q#": q["num"],
                "Question": r["question"],
                "Your Answer": f"({r['user'].upper()})",
                "Correct Answer": f"({r['correct'].upper()})",
                "Status": r["status"],
            })
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

    csv = df.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "📥 Download Results (CSV)",
        data=csv,
        file_name="up_pcs_prelims_results.csv",
        mime="text/csv",
    )

    if st.button("🔁 Restart Test", type="primary"):
        start_test(
            num_q=st.session_state.slider_num_q,
            shuffle=st.session_state.chk_shuffle,
            mode=st.session_state.mode,
        )
        st.rerun()


# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------
def main():
    st.set_page_config(page_title=PAGE_TITLE, layout="wide", page_icon="📝")
    st.markdown(
        """
        <style>
            .stApp { max-width: 1100px; margin: 0 auto; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    init_state()

    # Load bundled data once
    if st.session_state.questions is None:
        with st.spinner("Parsing the official PDF question booklet & answer key..."):
            st.session_state.questions = load_quiz()
        if not st.session_state.questions:
            st.error(
                "Could not parse the PDFs. Please make sure `pdf/question.pdf` "
                "and `pdf/answer.pdf` exist in the project folder."
            )
            st.stop()

    render_sidebar()

    if not st.session_state.started:
        render_intro()
    else:
        render_quiz()


if __name__ == "__main__":
    main()

