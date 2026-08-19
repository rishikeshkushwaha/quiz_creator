
import streamlit as st
import json
import ollama
from pypdf import PdfReader

# -----------------------------------------------------------------------------
# 1. SETUP & SYSTEM STYLING
# -----------------------------------------------------------------------------
st.set_page_config(page_title="Open Source MCQ Test Hub", layout="wide")

st.markdown("""
<style>
    .explanation-box {
        background-color: #f0f4f8;
        border-left: 5px solid #1f77b4;
        padding: 15px;
        border-radius: 4px;
        margin-top: 10px;
        color: #102a43;
    }
    .question-title {
        font-size: 1.2rem;
        font-weight: 600;
        margin-bottom: 15px;
    }
</style>
""")

# -----------------------------------------------------------------------------
# 2. HELPER FUNCTIONS: PDF EXTRACT & LOCAL LLM PARSING
# -----------------------------------------------------------------------------
def extract_text_from_pdf(uploaded_file):
    """Reads raw text content across all pages of the uploaded PDF."""
    reader = PdfReader(uploaded_file)
    full_text = ""
    for page in reader.pages:
        text = page.extract_text()
        if text:
            full_text += text + "\n"
    return full_text

def parse_text_to_mcq_json(raw_text, selected_model):
    """Uses a local open-source model via Ollama to structure data into a strict JSON format."""
    system_instruction = (
       "You are a strict data converter. Your job is to extract multiple-choice questions from the provided competitive exam text "
        "and structure them into a valid JSON array.\n\n"
        "Each object in the array MUST contain these exact keys:\n"
        "1. 'id' (integer)\n"
        "2. 'question' (string) - Include the core question text here.\n"
        "3. 'exam_tag' (string) - Extract the exact exam name and year metadata if present right below the options (e.g., 'U.P.P.C.S. (Mains) 2014; U.P. Lower Sub. (Mains) 2013'). If none is found, leave it as an empty string.\n"
        "4. 'options' (object with keys 'a', 'b', 'c', 'd')\n"
        "5. 'answer' (string) - Must strictly start with the prefix 'Ans. ' followed by the letter choice in parentheses, for example: 'Ans. (c)'\n"
        "6. 'explanation' (string) - Must strictly start with the prefix 'Ans. ' followed by the detailed text extracted from the explanation box.\n\n"
        "Do not include any introductory or concluding conversational text. Return ONLY the raw JSON array. "
        "If no multiple-choice questions are found on this page, return an empty array []."
    )
    
    prompt = f"Parse the following textbook content into the specified JSON format:\n\n{raw_text}"
    
    # Utilizing Ollama's local chat generation with enforced JSON format output
    response = ollama.chat(
        model=selected_model,
        messages=[
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": prompt}
        ],
        format="json", # Enforces structured JSON compilation natively
        options={"temperature": 0.1,"num_ctx": 8192,  # Forces Ollama to allocate a comfortable 8k context buffer memory window
                "num_predict": 2048}
    )
    
    try:
        content = response['message']['content']
        return json.loads(content)
    except Exception as e:
        st.error(f"Failed to parse model response into valid JSON: {e}")
        # Log content preview for user diagnostic review
        st.code(response['message']['content'][:500], language="json")
        return []

# -----------------------------------------------------------------------------
# 3. APP INTERFACE & STATE INITIALIZATION
# -----------------------------------------------------------------------------
st.title("📚 Local Open-Source MCQ Practice Hub")
st.subheader("Transform competitive exam PDFs into interactive practice tests locally.")

# Sidebar Configuration (Model selection replaces API key tracking)
with st.sidebar:
    st.header("Configuration")
    try:
        # Automatically pull models currently available on the local machine
        models_list = ollama.list()
        available_models = [m['name'] for m in models_list['models']]
    except Exception:
        available_models = ["llama3.2:latest", "mistral", "phi3"]
        st.sidebar.error("Could not connect to Ollama. Is the Ollama desktop app running?")
        
    selected_model = st.selectbox("Select Local Model:", options=available_models)
    st.write("---")
    
    if st.button("Reset / Clear Test Session"):
        for key in list(st.session_state.keys()):
            del st.session_state[key]
        st.rerun()

# Global session states tracking quiz progress
if "quiz_data" not in st.session_state:
    st.session_state.quiz_data = None
if "current_index" not in st.session_state:
    st.session_state.current_index = 0
if "score" not in st.session_state:
    st.session_state.score = 0
if "selected_option" not in st.session_state:
    st.session_state.selected_option = None
if "answer_submitted" not in st.session_state:
    st.session_state.answer_submitted = False

# -----------------------------------------------------------------------------
# 4. FILE UPLOADER & PROCESSING
# -----------------------------------------------------------------------------
if st.session_state.quiz_data is None:
    uploaded_file = st.file_uploader("Upload your practice PDF document", type=["pdf"])
    
    if uploaded_file is not None:
        with st.spinner("Extracting layouts using local model resources... Please wait."):
            raw_extracted_text = extract_text_from_pdf(uploaded_file)
            parsed_questions = parse_text_to_mcq_json(raw_extracted_text[:2000], selected_model)
            
            if parsed_questions:
                st.session_state.quiz_data = parsed_questions
                st.session_state.current_index = 0
                st.session_state.score = 0
                st.success(f"Successfully processed and generated {len(parsed_questions)} interactive questions!")
                st.rerun()
            else:
                st.error("No valid multi-choice structures could be verified. Check model outputs or try another model.")

# -----------------------------------------------------------------------------
# 5. TEST INTERACTION LAYER (QUIZ VIEW)
# -----------------------------------------------------------------------------
else:
    questions = st.session_state.quiz_data
    total_q = len(questions)
    idx = st.session_state.current_index
    
    progress_val = int(((idx) / total_q) * 100)
    st.progress(progress_val / 100)
    st.write(f"**Progress:** Question {idx + 1} of {total_q} | **Current Score:** {st.session_state.score}/{idx}")
    st.write("---")
    print(questions)
    if idx < total_q:
        current_q = questions[idx]
        
        st.markdown(f"<div class='question-title'>Q{idx+1}. {current_q.get('question', 'Missing Question Text')}</div>", unsafe_allowed_html=True)
        
        options_dict = current_q.get('options', {})
        formatted_options = []
        for opt_key in ['a', 'b', 'c', 'd']:
            if opt_key in options_dict:
                formatted_options.append(f"({opt_key}) {options_dict[opt_key]}")
            elif opt_key.upper() in options_dict:
                formatted_options.append(f"({opt_key}) {options_dict[opt_key.upper()]}")
        
        if not formatted_options:
            formatted_options = [f"({k}) {v}" for k, v in options_dict.items()]
            
        def select_option():
            st.session_state.selected_option = st.session_state[f"radio_q_{idx}"]

        user_choice = st.radio(
            "Choose your answer option:", 
            options=formatted_options, 
            index=None,
            key=f"radio_q_{idx}",
            on_change=select_option,
            disabled=st.session_state.answer_submitted
        )
        
        col1, col2 = st.columns([1, 5])
        
        with col1:
            if not st.session_state.answer_submitted:
                submit_btn = st.button("Submit Answer", use_container_width=True)
                if submit_btn:
                    if st.session_state.selected_option is None:
                        st.warning("Please make an option selection choice before checking accuracy validation.")
                    else:
                        st.session_state.answer_submitted = True
                        selected_letter = st.session_state.selected_option.strip()[1].lower()
                        correct_letter = str(current_q.get('answer', '')).strip().lower().replace("ans.", "").replace("(", "").replace(")", "")
                        
                        if selected_letter == correct_letter:
                            st.session_state.score += 1
                            st.toast("Correct Choice!", icon="✅")
                        else:
                            st.toast("Incorrect Choice!", icon="❌")
                        st.rerun()
            else:
                next_btn = st.button("Next Question", use_container_width=True)
                if next_btn:
                    st.session_state.current_index += 1
                    st.session_state.answer_submitted = False
                    st.session_state.selected_option = None
                    st.rerun()
                    
        if st.session_state.answer_submitted:
            selected_letter = st.session_state.selected_option.strip()[1].lower()
            correct_letter = str(current_q.get('answer', '')).strip().lower().replace("ans.", "").replace("(", "").replace(")", "")
            
            if selected_letter == correct_letter:
                st.success(f"🎉 Correct! The answer is ({correct_letter.upper()}).")
            else:
                st.error(f"😞 Incorrect. You selected ({selected_letter.upper()}). The correct answer is ({correct_letter.upper()}).")
            
            explanation = current_q.get('explanation', 'No explanation provided in original material file.')
            st.markdown(f"""
            <div class='explanation-box'>
                <strong>📝 Reference Explanation & Context:</strong><br/>
                {explanation}
            </div>
            """, unsafe_allowed_html=True)
            
    else:
        st.balloons()
        st.success("🏁 You have successfully completed this customized preparation test block!")
        st.metric(label="Final Correct Score", value=f"{st.session_state.score} / {total_q}")
        
        success_rate = (st.session_state.score / total_q) * 100
        st.write(f"Accuracy Rate Evaluator: **{success_rate:.1f}%**")
        
        if st.button("Restart Quiz Session Run"):
            st.session_state.current_index = 0
            st.session_state.score = 0
            st.session_state.answer_submitted = False
            st.session_state.selected_option = None
            st.rerun()
