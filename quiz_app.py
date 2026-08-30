import os
import re
import random

import pandas as pd
import streamlit as st
from pypdf import PdfReader

import storage_unified as storage

# -----------------------------------------------------------------------------
# CONSTANTS
# -----------------------------------------------------------------------------
TEST_SERIES_DIR = "pdf"
SOLVED_PDF = "pdf/UPPSC_Preview_General_Studies_Solved_Paper_2026.pdf"

TOTAL_FULL = 150
TOTAL_MARKS = 200
MARKS_CORRECT = TOTAL_MARKS / TOTAL_FULL          # 1.3333... per correct
MARKS_WRONG = MARKS_CORRECT / 3                   # 0.4444... per wrong

PAGE_TITLE = "UP-PCS Prelims 2026 Practice Hub"

# Modes of the exam
MODE_TEST = "Test Mode"
MODE_PRACTICE = "Practice Mode"

# Regexes used by the single-file test-series parser.
# Matches the start of a solved-style question block: a number at the start of a
# line followed by text beginning with a capital letter or a quote (mirrors the
# format used in the previous-year / solved-paper PDF).
_Q_START_RE = re.compile(r"(?m)^(\d{1,3})\.\s+(?=[A-Z\"\u2018\u201c])")
_OPT_RE = re.compile(r'\(([a-d])\)')
# Strong key headers only (standalone lines like "ANSWERS", "ANSWER KEY",
# "ANSWER & EXPLANATION"). Do NOT match generic question text.
_ANSWER_KEY_TOKEN_RE = re.compile(
    r"^\s*(?:answers?|answer\s*key|answer\s*&\s*explanation)\s*:?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# Answer pairs: accepts 1(c), 1. (c), 1.c, 1) c, 1 - c
_ANSWER_PAIR_RE = re.compile(
    r"(\d{1,3})\s*[.)\-]?\s*\(?\s*([a-d])\s*\)?",
    re.IGNORECASE,
)
# Devanagari (Hindi) characters, used to drop Hindi duplicates in bilingual PDFs.
_HINDI_RE = re.compile(r"[\u0900-\u097f]")


def score_params(questions):
    """Return (num_questions, marks_per_correct, marks_per_wrong, total_marks).

    Marking is fixed per question: +1.33 correct, -0.44 wrong, 0 for skipped.
    Total marks = number of questions x 1.33 (e.g. 150 -> 200 marks).
    """
    n = len(questions) or 1
    mc = MARKS_CORRECT      # 1.3333...
    mw = MARKS_WRONG        # 0.4444...
    total = round(n * MARKS_CORRECT)
    return n, mc, mw, total


def question_id(q):
    """Stable identifier for a question, used to link answers across attempts.

    Uses the source PDF basename + question number. For the solved paper the
    question number is unique within the whole book, so this is stable across
    page-range selections and retries. For uploaded/test-series PDFs the
    basename is used as the source key.
    """
    src = st.session_state.get("pdf_source") or "unknown"
    # For the solved paper, use a fixed key so page-range changes don't alter ids.
    if os.path.basename(SOLVED_PDF).lower() in src.lower():
        src = "uppsc_preview_2026"
    else:
        src = os.path.basename(src)
    src = re.sub(r"[^A-Za-z0-9]+", "_", src).strip("_").lower()
    return f"{src}_q{q['num']}"


def _topic_for_page(page):
    """Map a printed page number to a topic using the solved-paper index."""
    if page is None:
        return ""
    for entry in load_solved_index():
        if entry["Start page"] <= page <= entry["End page"]:
            return entry["Topic"]
    return ""


# -----------------------------------------------------------------------------
# TEXT CLEANING (fix PDF font artifacts from extraction)
# -----------------------------------------------------------------------------
def clean_text(s, collapse_newlines=False):
    """Clean garbled characters produced by the PDF's custom font mapping."""
    s = re.sub(r"Adju\s*ment", "Adjustment", s)
    s = re.sub(r"(?<!s)atement(s)?\b", r"statement\1", s)
    s = re.sub(r"(?<!s)tartup\b", "startup", s)
    s = re.sub(r"\bV\s+oters", "Voters", s)
    s = re.sub(r"Augu\s+", "August", s)
    s = s.replace("\u00e6", "'").replace("\u00c6", "'")
    s = s.replace("\u00fb", "-").replace("\u00db", "-")
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
    return bool(re.search(r"[\u0900-\u097f]", line))


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


def is_noise_strict(ln):
    """Strict noise check used when splitting question blocks."""
    return is_noise(ln)


# Subfolders (or partial names) that are image-scanned PDFs and not extractable.
_IMAGE_ONLY_DIRS = ("gs world", "gs_world", "gsworld")


def _is_extractable_file(path):
    """Return False for image-only scanned PDFs and known non-extractable files."""
    parts = os.path.normpath(path).lower().split(os.sep)
    if any(im in p for im in _IMAGE_ONLY_DIRS for p in parts):
        return False
    low = os.path.basename(path).lower()
    if low in ("question.pdf", "answer.pdf"):
        return False
    if low == os.path.basename(SOLVED_PDF).lower():
        return False
    return True


def discover_test_series():
    """Return a sorted list of (display_label, absolute_path) of every PDF in
    the pdf/ folder (including subfolders such as Drishti, Next Ias),
    excluding the solved-paper PDF, image-only scanned PDFs (e.g. GS World),
    and the legacy question/answer PDFs."""
    found = []
    for root, _dirs, files in os.walk(TEST_SERIES_DIR):
        for f in sorted(files):
            if not f.lower().endswith(".pdf"):
                continue
            full = os.path.join(root, f)
            if not _is_extractable_file(full):
                continue
            found.append((os.path.relpath(full, TEST_SERIES_DIR), os.path.abspath(full)))
    return found


def _extract_all_lines(path=None):
    """Return the raw extracted text lines of a PDF (single file)."""
    reader = PdfReader(path)
    lines = []
    for page in reader.pages:
        text = page.extract_text() or ""
        lines.extend(text.split("\n"))
    return lines


def _extract_answer_key(text):
    """Return {int: letter} by finding the ANSWERS-key block and parsing the
    `1(c) 2(d) ...` tokens inside it.

    Uses the LAST key header (the real answer key sits at the end of the PDF,
    after the question pages)."""
    matches = list(_ANSWER_KEY_TOKEN_RE.finditer(text))
    if not matches:
        return {}
    token = matches[-1]
    seg = text[token.start():token.start() + 4000]
    pairs = _ANSWER_PAIR_RE.findall(seg)
    return {int(n): let.lower() for n, let in pairs}


def _find_blocks(lines):
    """Locate each question block via its leading number + 4 option markers.

    Returns a list of (num, list_of_lines) tuples. This line-scanning approach
    is more robust than splitting only on the next question number: a block is
    defined by collecting 4 `(a)-(d)` option markers, which reliably captures
    the full question even when numbered sub-parts or unusual layout interfere.
    Stops at the ANSWERS-key block (if present)."""
    cleaned = []
    for ln in lines:
        if is_noise_strict(ln):
            continue
        # Drop Devanagari (Hindi) lines in bilingual PDFs so only English stays.
        if _HINDI_RE.search(ln):
            continue
        cleaned.append(ln)

    blocks = []
    seen = set()
    i, n = 0, len(cleaned)
    while i < n:
        m = re.match(r"^\s*(\d{1,3})\.\s*(.*)$", cleaned[i])
        if m:
            num = int(m.group(1))
            rest = m.group(2)
            # Accept a question start if the same line has text starting with a
            # letter, OR if the number stands alone on its line (the question
            # text follows on the next line). This handles layouts where the
            # PDF extraction splits "20. Question..." into "20. " + text.
            if re.match(r"^[A-Za-z'\u2018\u2019\u0022\u201C\u201D]", rest) or rest == "":
                j, count = i, 0
                while j < n:
                    # Stop if we hit the ANSWERS-key block.
                    if _ANSWER_KEY_TOKEN_RE.search(cleaned[j]):
                        break
                    count += len(re.findall(r"\([a-d]\)", cleaned[j]))
                    if count >= 4:
                        break
                    j += 1
                if count >= 4:
                    # Keep only the FIRST occurrence of each question number.
                    # Bilingual PDFs repeat each question (English + Hindi); the
                    # English block appears first, so the Hindi duplicate is dropped.
                    if num not in seen:
                        seen.add(num)
                        blocks.append((num, cleaned[i:j + 1]))
                    i = j + 1
                    continue
        i += 1
    return blocks


def _parse_question_block(num, lines):
    """Build {num, question, options{..}} for one block."""
    text = "\n".join(lines)
    matches = list(_OPT_RE.finditer(text))
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
    if not qtext:
        return None
    return {"num": num, "question": qtext, "options": options}


# --- Solved-style parse (single file: Q -> Ans. -> explanation) --------------
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
    matches = list(_Q_START_RE.finditer(text))
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

    explanation = clean_text(
        "\n".join(lines[ans_idx + 1:]), collapse_newlines=True
    )
    head = "\n".join(lines[:ans_idx])
    opt_matches = list(_OPT_RE.finditer(head))
    if len(opt_matches) < 4:
        return None
    options = {}
    for i, om in enumerate(opt_matches[:4]):
        letter = om.group(1)
        ostart = om.end()
        oend = opt_matches[i + 1].start() if i < 3 else len(head)
        options[letter] = clean_text(head[ostart:oend], collapse_newlines=True)

    qtext = head[:opt_matches[0].start()].strip()
    qtext = re.sub(r"^\s*\d{1,3}\.\s*", "", qtext)
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


# --- Top-level loaders -------------------------------------------------------
@st.cache_data(show_spinner=False)
def load_test_series(path):
    """Parse a single test-series PDF into a list of question dicts.

    Handles both formats:
      * question pages + a later `ANSWERS` key block (no explanations), and
      * solved style with per-question `Ans.` + explanation blocks.
    """
    lines = _extract_all_lines(path)
    text = "\n".join(lines)

    # Format 1: embedded ANSWERS key in the same PDF.
    key = _extract_answer_key(text)
    questions = []
    for num, block_lines in _find_blocks(lines):
        q = _parse_question_block(num, block_lines)
        if q:
            if num in key:
                q["answer"] = key[num]
            questions.append(q)
    # Drop any question that didn't get an answer key (avoids KeyError later).
    questions = [q for q in questions if "answer" in q]
    if questions:
        return questions

    # Format 2: solved style with Ans. + explanations.
    solved = parse_solved_lines(lines)
    if solved:
        return solved
    return questions


def extract_pages(reader, start_page, end_page):
    """Return the raw extracted text for pages [start_page, end_page] (1-based).

    Returns a list of (printed_page_number, line) tuples so that each question
    can later be tagged with its source page and topic.
    """
    lines = []
    n = len(reader.pages)
    s = max(1, min(int(start_page), n))
    e = max(s, min(int(end_page), n))
    for p in range(s - 1, e):
        text = reader.pages[p].extract_text() or ""
        for ln in text.split("\n"):
            if _is_one_liner(ln):
                continue
            lines.append((p + 1, ln))
    return lines

@st.cache_data(show_spinner=False)
def load_solved_index():
    """Extract topic and inclusive printed-page ranges from the PDF index."""
    reader = PdfReader(SOLVED_PDF)
    index_lines = []
    for page_number in (3, 4):
        if page_number <= len(reader.pages):
            text = reader.pages[page_number - 1].extract_text() or ""
            index_lines.extend(text.splitlines())

    entries = []
    section = ""
    pending = []
    range_re = re.compile(r"(\d{1,4})\s*-\s*(\d{1,4})\s*$")
    section_names = {
        "Indian Polity and Governance",
        "Indian History",
        "General Geography",
        "General Science",
        "Indian Economy",
        "Environment and Ecology",
        "Current Affairs",
    }

    for raw_line in index_lines:
        line = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", raw_line)
        line = re.sub(r"\s+", " ", line).strip()
        if not line or line.isdigit():
            continue
        line = re.sub(r"^q\s*", "", line, flags=re.IGNORECASE)
        line = line.strip()

        if line in section_names:
            section = line
            pending = []
            continue

        pending.append(line)
        match = range_re.search(line)
        if not match:
            continue

        topic = re.sub(r"\s+", " ", " ".join(pending[:-1] + [line[:match.start()]]))
        topic = topic.strip(" -")
        if topic:
            entries.append(
                {
                    "Section": section,
                    "Topic": topic,
                    "Start page": int(match.group(1)),
                    "End page": int(match.group(2)),
                }
            )
        pending = []

    return entries


@st.cache_data(show_spinner=False)
def load_solved(start_page, end_page):
    """Parse the solved-paper PDF over the given inclusive page range.

    Each question is tagged with its printed source page and topic.
    """
    reader = PdfReader(SOLVED_PDF)
    page_lines = extract_pages(reader, start_page, end_page)
    questions = parse_solved_lines([ln for _p, ln in page_lines])

    # Map each question to its source page by locating its number in the
    # page-annotated line stream.
    page_by_num = {}
    for p, ln in page_lines:
        m = re.match(r"^\s*(\d{1,3})\.\s+", ln)
        if m:
            page_by_num.setdefault(int(m.group(1)), p)

    for q in questions:
        q["source_page"] = page_by_num.get(q["num"])
        q["topic"] = _topic_for_page(q["source_page"])
    return questions


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
        "pdf_source": "No test series selected",
        "test_series": [],   # [ (label, path), ...] discovered in pdf/
        "mode": MODE_TEST,   # MODE_TEST or MODE_PRACTICE
        "profile": None,     # dict {id, name} of the active profile
        "attempt_id": None,  # current persistent attempt id
        "attempt_type": "original",
        "start_page": None,
        "end_page": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _question_id_map(questions):
    """Return {question_id: question} for the current question list."""
    return {question_id(q): q for q in questions}


def start_test(num_q, shuffle, mode, question_ids=None, attempt_type="original",
               parent_attempt_id=None):
    """Start a test/practice session and persist it as a new attempt.

    If `question_ids` is provided (resume/retry), those questions are used in
    the given order instead of the full question list.
    """
    questions = st.session_state.questions
    qid_map = _question_id_map(questions)

    if question_ids is None:
        order = list(range(len(questions)))
        if shuffle:
            random.shuffle(order)
        num_q = min(num_q, len(questions))
        order = order[:num_q]
        ordered_qids = [question_id(questions[i]) for i in order]
    else:
        # question_ids already in the desired order; map back to indices.
        ordered_qids = [qid for qid in question_ids if qid in qid_map]
        order = [questions.index(qid_map[qid]) for qid in ordered_qids]

    st.session_state.order = order
    st.session_state.idx = 0
    st.session_state.selected = None
    st.session_state.responses = {}
    st.session_state.nav_jump = 0
    st.session_state.mode = mode
    st.session_state.started = True
    st.session_state.attempt_type = attempt_type

    # Persist the attempt (only if a profile is active).
    if st.session_state.profile:
        st.session_state.attempt_id = storage.create_attempt(
            profile_id=st.session_state.profile["id"],
            source_pdf=st.session_state.pdf_source,
            mode=mode,
            question_ids=ordered_qids,
            start_page=st.session_state.start_page,
            end_page=st.session_state.end_page,
            attempt_type=attempt_type,
            parent_attempt_id=parent_attempt_id,
        )
    else:
        st.session_state.attempt_id = None


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
def _render_profile_section():
    """Profile selection / creation and unfinished-session resume."""
    profiles = storage.list_profiles()
    names = [p["name"] for p in profiles]

    st.caption("**👤 Profile**")
    if st.session_state.profile:
        st.success(f"Signed in as **{st.session_state.profile['name']}**")
        if st.button("Sign out", use_container_width=True, key="btn_sign_out"):
            st.session_state.profile = None
            st.session_state.attempt_id = None
            st.rerun()
    else:
        if names:
            sel = st.selectbox("Select profile", names, key="profile_sel")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("Use", use_container_width=True, key="btn_use_profile"):
                    st.session_state.profile = next(
                        p for p in profiles if p["name"] == sel
                    )
                    st.rerun()
            with c2:
                st.caption("or create:")
        new_name = st.text_input("New profile name", key="profile_new")
        if st.button("➕ Create profile", use_container_width=True, 
                       disabled=not new_name.strip(), key="btn_create_profile"):
            try:
                st.session_state.profile = storage.get_or_create_profile(new_name)
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))

    # Resume unfinished attempts for the active profile.
    if st.session_state.profile:
        unfinished = storage.list_unfinished_attempts(st.session_state.profile["id"])
        if unfinished:
            st.divider()
            st.caption("**⏳ Resume unfinished session**")
            labels = [
                f"{a['source_pdf']} · {a['mode']} · {a['question_count']} Q"
                for a in unfinished
            ]
            pick = st.selectbox("Unfinished sessions", labels, key="resume_sel")
            if st.button("▶️ Resume", use_container_width=True, key="btn_resume"):
                _resume_attempt(unfinished[labels.index(pick)])


def _resume_attempt(attempt):
    """Restore an in-progress attempt into session state."""
    qid_map = _question_id_map(st.session_state.questions or [])
    ordered_qids = storage.get_attempt_question_ids(attempt["id"])
    answers = storage.get_attempt_answers(attempt["id"])

    # Rebuild order and responses from stored question ids.
    order = []
    responses = {}
    for pos, qid in enumerate(ordered_qids):
        q = qid_map.get(qid)
        if q is None:
            continue
        order.append(st.session_state.questions.index(q))
        ans = answers.get(qid)
        if ans:
            responses[pos] = {
                "question": q["question"],
                "user": ans["selected_answer"],
                "correct": ans["correct_answer"],
                "status": ans["status"],
            }

    st.session_state.order = order
    st.session_state.responses = responses
    st.session_state.idx = 0
    st.session_state.selected = None
    st.session_state.nav_jump = 0
    st.session_state.mode = attempt["mode"]
    st.session_state.started = True
    st.session_state.attempt_id = attempt["id"]
    st.session_state.attempt_type = attempt["attempt_type"]
    st.session_state.start_page = attempt["start_page"]
    st.session_state.end_page = attempt["end_page"]
    st.rerun()


def render_sidebar():
    n_total = len(st.session_state.questions) if st.session_state.questions else 0
    with st.sidebar:
        st.header("⚙️ Test Settings")

        _render_profile_section()

        st.caption("**Select a Test Series PDF**")
        if not st.session_state.test_series:
            st.info("No test-series PDFs found. Place PDFs in the `pdf/` folder.")
        else:
            labels = [label for label, _path in st.session_state.test_series]
            idx = st.selectbox(
                "Available papers:",
                options=list(range(len(labels))),
                format_func=lambda i: labels[i],
                key="ts_sel",
            )
            if st.button("📂 Load Selected PDF", use_container_width=True, key="btn_load_selected_pdf"):
                label, path = st.session_state.test_series[idx]
                with st.spinner(f"Parsing {label}..."):
                    qs = load_test_series(path)
                if qs:
                    st.session_state.questions = qs
                    st.session_state.pdf_source = label
                    st.session_state.start_page = None
                    st.session_state.end_page = None
                    st.session_state.order = None
                    st.session_state.idx = 0
                    st.session_state.started = False
                    st.session_state.selected = None
                    st.session_state.responses = {}
                    st.session_state.nav_jump = 0
                    st.session_state.attempt_id = None
                    st.session_state.pop("slider_num_q", None)
                    st.rerun()
                else:
                    st.error("Could not parse the selected PDF. Please check the format.")

        with st.expander("📤 Upload Your Own Test-Series PDF", expanded=False):
            st.caption(
                "Upload a single PDF. It may contain an embedded `ANSWERS` key block "
                "(`1 (c) 2 (d) ...`) and/or solved-style `Ans.` + explanation blocks."
            )
            up_file = st.file_uploader("Test-series PDF", type=["pdf"], key="up_pdf")
            if st.button("📂 Load Uploaded PDF", use_container_width=True, disabled=(up_file is None), key="btn_load_uploaded_pdf"):
                with st.spinner("Parsing the uploaded PDF..."):
                    qs = load_test_series_bytes(up_file.getvalue())
                if qs:
                    st.session_state.questions = qs
                    st.session_state.pdf_source = up_file.name
                    st.session_state.start_page = None
                    st.session_state.end_page = None
                    st.session_state.order = None
                    st.session_state.idx = 0
                    st.session_state.started = False
                    st.session_state.selected = None
                    st.session_state.responses = {}
                    st.session_state.nav_jump = 0
                    st.session_state.attempt_id = None
                    st.session_state.pop("slider_num_q", None)
                    st.rerun()
                else:
                    st.error("Could not parse the uploaded PDF. Please check the format.")

        with st.expander("📜 Load Previous Year Questions (PYQ)", expanded=True):
            st.caption(
                f"Read questions from the solved paper "
                f"`{os.path.basename(SOLVED_PDF)}` by page range."
            )
            try:
                _npages = len(PdfReader(SOLVED_PDF).pages)
            except Exception:
                _npages = 0

            try:
                index_rows = load_solved_index()
            except Exception as exc:
                index_rows = []
                st.warning(f"Could not read the PDF index: {exc}")
            if index_rows:
                st.caption("Index: choose a topic's printed page range for this test or practice session.")
                st.dataframe(
                    pd.DataFrame(index_rows),
                    hide_index=True,
                    height=420,
                    use_container_width=True,
                    column_config={
                        "Start page": st.column_config.NumberColumn(format="%d"),
                        "End page": st.column_config.NumberColumn(format="%d"),
                    },
                )

            c_a, c_b = st.columns(2)
            with c_a:
                sp = st.number_input("Start page", min_value=1, max_value=max(1, _npages), value=1)
            with c_b:
                ep = st.number_input("End page", min_value=1, max_value=max(1, _npages), value=min(_npages, 5))
            if st.button("📂 Load PYQ Pages", use_container_width=True):
                with st.spinner("Parsing solved paper pages..."):
                    qs = load_solved(int(sp), int(ep))
                if qs:
                    st.session_state.questions = qs
                    st.session_state.pdf_source = f"PYQ pages {sp}-{ep} ({os.path.basename(SOLVED_PDF)})"
                    st.session_state.start_page = int(sp)
                    st.session_state.end_page = int(ep)
                    st.session_state.order = None
                    st.session_state.idx = 0
                    st.session_state.started = False
                    st.session_state.selected = None
                    st.session_state.responses = {}
                    st.session_state.nav_jump = 0
                    st.session_state.attempt_id = None
                    st.session_state.pop("slider_num_q", None)
                    st.rerun()
                else:
                    st.error("No questions found in that page range. Try a different range.")

        if st.session_state.questions:
            st.divider()
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

        st.divider()
        st.caption("**Marking Scheme** (UP-PCS Prelims)")
        st.caption(f"✅ **+{MARKS_CORRECT:.3f}** per correct")
        st.caption(f"❌ **-{MARKS_WRONG:.4f}** per incorrect")
        if n_total:
            st.caption(f"Total: **{round(n_total * MARKS_CORRECT)} marks** / **{n_total} questions**")
        st.caption("0 marks for skipped / unattempted")
        st.divider()

        if st.session_state.questions:
            if n_total > 1:
                step = 5 if n_total >= 10 else 1
                st.slider(
                    "Number of questions",
                    min_value=1,
                    max_value=n_total,
                    value=n_total,
                    step=step,
                    key="slider_num_q",
                )
            else:
                st.session_state.setdefault("slider_num_q", n_total)
            st.checkbox("Shuffle question order", value=False, key="chk_shuffle")
            st.divider()
        if st.button("🔄 Reset / New Test", use_container_width=True, key="btn_reset_test"):
            reset_app()


# --- Overhead loader for uploaded bytes (writes to temp) ---------------------
@st.cache_data(show_spinner=False)
def load_test_series_bytes(pdf_bytes: bytes):
    """Parse a test-series PDF from raw uploaded bytes."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name
    try:
        return load_test_series(tmp_path)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


# -----------------------------------------------------------------------------
# UI: INTRO SCREEN
# -----------------------------------------------------------------------------
def _render_dashboard():
    """Show profile stats and recent attempt history."""
    if not st.session_state.profile:
        return
    profile = st.session_state.profile
    stats = storage.get_profile_stats(profile["id"])
    completed = storage.list_completed_attempts(profile["id"], limit=20)

    st.divider()
    st.subheader(f"📊 Progress — {profile['name']}")

    if stats["total_attempts"] == 0:
        st.info("No completed attempts yet. Start a test to begin tracking progress.")
        return

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Attempts", stats["total_attempts"])
    c2.metric("Test Mode", stats["test_attempts"] or 0)
    c3.metric("Practice Mode", stats["practice_attempts"] or 0)
    c4.metric("Accuracy", f"{stats['accuracy']:.1f}%")

    if completed:
        st.caption("**Recent attempts**")
        rows = []
        for a in completed:
            # Calculate accuracy for this attempt
            ans = storage.get_attempt_answers(a["id"])
            correct = sum(1 for v in ans.values() if v["status"] == "Correct")
            wrong = sum(1 for v in ans.values() if v["status"] == "Wrong")
            attempted = correct + wrong
            acc = (correct / attempted * 100) if attempted else 0.0
            rows.append({
                "Source": a["source_pdf"],
                "Mode": a["mode"],
                "Type": a["attempt_type"],
                "Questions": a["question_count"],
                "Accuracy": f"{acc:.1f}%",
                "Completed": (a["completed_at"] or "")[:16].replace("T", " "),
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


def render_intro():
    st.title(f"📚 {PAGE_TITLE}")
    st.subheader("Practice Test Series - 2026 · General Studies-I (English Version)")

    n_total = len(st.session_state.questions) if st.session_state.questions else 0
    src = st.session_state.pdf_source

    if st.session_state.questions is None:
        st.info(
            "Select a test-series PDF from the **sidebar** (or upload your own) "
            "to load questions, then press Start."
        )
        _render_dashboard()
        return

    mode_desc = (
        "✅ Answers are revealed instantly after each submission."
        if st.session_state.mode_choice == MODE_PRACTICE
        else "🔒 Answers are revealed only after the full test is submitted."
    )

    st.markdown(
        f"""
        **Active paper:** `{src}`
        **Selected mode:** `{st.session_state.mode_choice}` — {mode_desc}

        This app parses a single test-series PDF — question pages, an embedded
        `ANSWERS` key block, and (if present) solved-style `Ans.` explanations —
        running **100% offline** with no LLM required.

        - **{n_total} questions** extracted
        - **Marking:** +{MARKS_CORRECT:.3f} for correct, -{MARKS_WRONG:.4f} for incorrect, 0 for skipped
        - **Max score:** {round(n_total * MARKS_CORRECT)} marks
        - Jump to **any question** anytime (Previous / Next / dropdown)
        - Full answer review table + **CSV export** at the end
        """
    )

    st.divider()
    st.info("Configure the number of questions, mode & shuffling in the **sidebar**, then press Start.")
    if st.button("🚀 Start Test", type="primary", use_container_width=True, key="btn_start_test"):
        start_test(
            num_q=st.session_state.slider_num_q,
            shuffle=st.session_state.chk_shuffle,
            mode=st.session_state.mode_choice,
        )
        st.rerun()

    _render_dashboard()


# -----------------------------------------------------------------------------
# UI: QUIZ SCREEN
# -----------------------------------------------------------------------------
def render_quiz():
    questions = st.session_state.questions
    order = st.session_state.order
    total = len(order)
    idx = st.session_state.idx

    if int(idx) < total:
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
                    status = "Correct" if sel == q["answer"] else "Wrong"
                    responses[idx] = {
                        "question": q["question"],
                        "user": sel,
                        "correct": q["answer"],
                        "status": status,
                    }
                    # Persist the answer if an attempt is active.
                    if st.session_state.attempt_id:
                        storage.save_answer(
                            attempt_id=st.session_state.attempt_id,
                            question_id=question_id(q),
                            selected_answer=sel,
                            correct_answer=q["answer"],
                            status=status,
                        )
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
            # Show explanation if available (solved-paper style PDFs)
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

    # Mark the attempt complete once (idempotent).
    if st.session_state.attempt_id:
        storage.complete_attempt(st.session_state.attempt_id)

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

    # --- Retry options -----------------------------------------------------
    if st.session_state.profile and st.session_state.attempt_id:
        st.subheader("🔁 Re-attempt")
        wrong_ids = storage.get_wrong_question_ids(st.session_state.attempt_id)
        skipped_ids = storage.get_skipped_question_ids(st.session_state.attempt_id)
        unmastered_ids = storage.get_unmastered_question_ids(st.session_state.attempt_id)
        all_ids = storage.get_attempt_question_ids(st.session_state.attempt_id)

        r1, r2, r3, r4 = st.columns(4)
        with r1:
            if st.button(f"❌ Wrong ({len(wrong_ids)})", use_container_width=True,
                         disabled=not wrong_ids, key="btn_retry_wrong"):
                _start_retry(wrong_ids, "retry_wrong")
        with r2:
            if st.button(f"⏭️ Skipped ({len(skipped_ids)})", use_container_width=True,
                         disabled=not skipped_ids, key="btn_retry_skipped"):
                _start_retry(skipped_ids, "retry_skipped")
        with r3:
            if st.button(f"⚠️ Wrong+Skipped ({len(unmastered_ids)})",
                         use_container_width=True, disabled=not unmastered_ids, key="btn_retry_unmastered"):
                _start_retry(unmastered_ids, "retry_unmastered")
        with r4:
            if st.button(f"🔁 Full test ({len(all_ids)})", use_container_width=True, key="btn_retry_full"):
                _start_retry(all_ids, "retry_full")
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

    st.button("📖 Open Selected Question", use_container_width=True, on_click=_open_review, key="btn_open_review")

    st.divider()
    st.subheader("📋 Detailed Review")

    rows = []
    for idx in range(total):
        q = questions[order[idx]]
        r = responses.get(idx)
        if r is None:
            rows.append({
                "Q#": q["num"],
                "Topic": q.get("topic", ""),
                "Question": q["question"],
                "Your Answer": "—",
                "Correct Answer": f"({q['answer'].upper()})",
                "Status": "Skipped",
            })
        else:
            rows.append({
                "Q#": q["num"],
                "Topic": q.get("topic", ""),
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

    if st.button("🔁 Restart Test", type="primary", key="btn_restart_test"):
        start_test(
            num_q=st.session_state.slider_num_q,
            shuffle=st.session_state.chk_shuffle,
            mode=st.session_state.mode,
        )
        st.rerun()

    if st.button("🏠 Return to Dashboard", use_container_width=True, key="btn_return_dashboard"):
        # Reset test state but keep profile and questions loaded
        st.session_state.started = False
        st.session_state.order = None
        st.session_state.idx = 0
        st.session_state.selected = None
        st.session_state.responses = {}
        st.session_state.nav_jump = 0
        st.session_state.attempt_id = None
        st.session_state.attempt_type = "original"
        st.rerun()


def _start_retry(question_ids, retry_type):
    """Start a new attempt containing only the given question ids."""
    start_test(
        num_q=len(question_ids),
        shuffle=False,
        mode=st.session_state.mode,
        question_ids=question_ids,
        attempt_type=retry_type,
        parent_attempt_id=st.session_state.attempt_id,
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
    storage.init_db()

    # Discover test-series PDFs on first run
    if st.session_state.questions is None and not st.session_state.test_series:
        st.session_state.test_series = discover_test_series()

    render_sidebar()

    if st.session_state.questions is None:
        # Nothing loaded yet: show the intro/landing screen only
        render_intro()
    elif not st.session_state.started:
        render_intro()
    else:
        render_quiz()


if __name__ == "__main__":
    main()

