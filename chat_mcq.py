import streamlit as st
import re
import time

# -------------------------------
# FUNCTION: PARSE QUESTIONS
# -------------------------------
def clean_text(text):
    # Remove unwanted lines
    text = re.sub(r"CLICK HERE FOR FREE BOOKS.*?\n", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"UPPCS.*?\d+\s*YCT", "", text)

    return text


def parse_questions(text):
    text = clean_text(text)

    questions = []

    # -------------------------------
    # MATCH: (Question Number → Ans)
    # -------------------------------
    pattern = r"(\d{1,3}\..*?Ans\.?\s*[:\-]?\s*\(?[a-d]\)?.*?)(?=\n\d{1,3}\.\s|$)"

    blocks = re.findall(pattern, text, re.DOTALL | re.IGNORECASE)

    for block in blocks:

        block = block.strip()

        # -------------------------------
        # EXTRACT ANSWER
        # -------------------------------
        ans_match = re.search(r"Ans\.?\s*[:\-]?\s*\(?([a-d])\)?", block, re.IGNORECASE)
        correct = ans_match.group(1).lower() if ans_match else None

        # -------------------------------
        # REMOVE ANSWER PART
        # -------------------------------
        block_clean = re.split(r"Ans\.?", block)[0].strip()

        # -------------------------------
        # EXTRACT OPTIONS USING POSITION
        # -------------------------------
        opt_matches = list(re.finditer(r"\([a-d]\)", block_clean))

        if len(opt_matches) < 4:
            continue  # skip broken (rare now)

        options = []
        for i in range(4):
            start = opt_matches[i].end()
            end = opt_matches[i+1].start() if i < 3 else len(block_clean)
            options.append(block_clean[start:end].strip())

        # -------------------------------
        # EXTRACT QUESTION TEXT
        # -------------------------------
        question = block_clean[:opt_matches[0].start()].strip()

        questions.append({
            "question": question,
            "options": options,
            "answer": correct
        })

    return questions

file = "text\\2019_geo.txt"
# -------------------------------
# LOAD FILE
# -------------------------------
@st.cache_data
def load_data():
    with open(file, "r", encoding="utf-8") as f:
        return f.read()

text = load_data()
questions = parse_questions(text)

# Limit to 150 questions
questions = questions[:30]


# -------------------------------
# SESSION STATE
# -------------------------------
if "start_time" not in st.session_state:
    st.session_state.start_time = None

if "submitted" not in st.session_state:
    st.session_state.submitted = False

if "responses" not in st.session_state:
    st.session_state.responses = {}


# -------------------------------
# UI
# -------------------------------
st.title("📝 Online Exam System")

if st.button("Start Exam"):
    st.session_state.start_time = time.time()
    st.session_state.submitted = False


# -------------------------------
# TIMER (e.g., 60 minutes)
# -------------------------------
EXAM_DURATION = 30*60  # 30 minutes

if st.session_state.start_time:
    elapsed = time.time() - st.session_state.start_time
    remaining = int(EXAM_DURATION - elapsed)

    if remaining <= 0:
        st.warning("⏰ Time's up! Submitting automatically...")
        st.session_state.submitted = True
    else:
        mins, secs = divmod(remaining, 60)
        st.info(f"⏳ Time Remaining: {mins} min {secs} sec")


# -------------------------------
# DISPLAY QUESTIONS
# -------------------------------
import pandas as pd
import random

# Shuffle questions (optional)
if "shuffled" not in st.session_state:
    random.shuffle(questions)
    st.session_state.shuffled = True
# Initialize session state
if "responses" not in st.session_state:
    st.session_state.responses = {}

if "submitted" not in st.session_state:
    st.session_state.submitted = False


# -------------------------------
# DISPLAY QUESTIONS (NO PRESELECT)
# -------------------------------
if st.session_state.start_time and not st.session_state.submitted:

    for idx, q in enumerate(questions):

        st.write(f"**Q{idx+1}. {q['question']}**")

        options = ["-- Select --"] + q["options"]

        selected = st.radio(
            f"Answer for Q{idx+1}",
            range(len(options)),
            format_func=lambda x: options[x],
            index=0,
            key=f"q_{idx}"
        )

        if selected != 0:
            st.session_state.responses[idx] = selected - 1


    # ✅ ONLY ONE BUTTON (outside loop)
if st.button("Submit Exam", key="submit_exam"):
    st.session_state.submitted = True


# -------------------------------
# EVALUATION
# -------------------------------
if st.session_state.submitted:

    total_questions = len(questions)
    correct_count = 0
    wrong_count = 0
    skipped_count = 0

    marks = 0

    results_data = []

    for idx, q in enumerate(questions):

        user_idx = st.session_state.responses.get(idx, None)

        correct_index = ord(q["answer"]) - ord('a') if q["answer"] else None

        # -------------------------------
        # CHECK STATUS
        # -------------------------------
        if user_idx is None:
            status = "Skipped"
            skipped_count += 1
            user_ans = "Not Answered"

        elif user_idx == correct_index:
            status = "Correct"
            correct_count += 1
            marks += 2
            user_ans = q["options"][user_idx]

        else:
            status = "Wrong"
            wrong_count += 1
            marks -= (2/3)
            user_ans = q["options"][user_idx]

        correct_ans = q["options"][correct_index] if correct_index is not None else "N/A"

        results_data.append({
            "Question": q["question"],
            "Your Answer": user_ans,
            "Correct Answer": correct_ans,
            "Status": status
        })

    # -------------------------------
    # CALCULATIONS
    # -------------------------------
    attempted = correct_count + wrong_count

    accuracy = (correct_count / attempted * 100) if attempted > 0 else 0

    # -------------------------------
    # DISPLAY RESULT
    # -------------------------------
    st.subheader("📊 Exam Analysis")

    st.write(f"**Total Questions:** {total_questions}")
    st.write(f"**Attempted:** {attempted}")
    st.write(f"**Correct:** {correct_count}")
    st.write(f"**Wrong:** {wrong_count}")
    st.write(f"**Skipped:** {skipped_count}")

    st.write(f"**Score:** {round(marks, 2)}")

    st.write(f"**Accuracy:** {round(accuracy, 2)}%")

    # -------------------------------
    # PERFORMANCE MESSAGE
    # -------------------------------
    if accuracy >= 80:
        st.success("Excellent performance 🎯")
    elif accuracy >= 60:
        st.info("Good performance 👍")
    elif accuracy >= 40:
        st.warning("Average performance ⚠️")
    else:
        st.error("Needs improvement ❗")

    # -------------------------------
    # EXPORT TO EXCEL
    # -------------------------------
    import pandas as pd

    df = pd.DataFrame(results_data)

    file_name = "exam_results.xlsx"
    df.to_excel(file_name, index=False)

    with open(file_name, "rb") as f:
        st.download_button(
            label="📥 Download Results",
            data=f,
            file_name=file_name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    # -------------------------------
    # SHOW TABLE
    # -------------------------------
    st.write("### Detailed Report")
    st.dataframe(df)

