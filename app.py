import streamlit as st
import pandas as pd
import sqlite3, os, datetime, io, hashlib, shutil
import plotly.express as px


# ---------- File Paths ----------
DB_PATH = "data/app.db"
CURRENT_USER_FILE = "data/current_user.txt"
USERS_CSV_PATH = "data/users.csv"
FEEDBACK_LOG_PATH = "data/feedback_log.csv"
USERS_EXPORT_PATH = "data/users_export.csv"
USER_ACTIVITY_PATH = "data/user_activity.csv"


# ---------- Database Connection ----------
def get_db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


# ---------- DB & CSV Migration Helpers ----------
def ensure_users_table():
    conn = get_db_conn()
    cur = conn.cursor()
    # Check if users table exists
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
    if not cur.fetchone():
        # create table fresh
        cur.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE,
                password TEXT,
                created_at TEXT
            )
            """
        )
        conn.commit()
        conn.close()
        return

    # table exists; get columns
    cur.execute("PRAGMA table_info(users)")
    cols = [r[1] for r in cur.fetchall()]
    needed = set(["id", "username", "password", "created_at"])
    if set(cols) == needed or ("username" in cols and "password" in cols and "created_at" in cols):
        # table already has the needed columns (or at least username/password/created_at)
        conn.close()
        return

    # Perform migration: create a new table, copy relevant columns, drop old, rename
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users_new (
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE,
            password TEXT,
            created_at TEXT
        )
        """
    )

    # Try to copy username and password; set created_at to existing column if present else current time
    # Build select list depending on existing cols
    select_cols = []
    select_cols.append("COALESCE(username,'') AS username")
    select_cols.append("COALESCE(password,'') AS password")
    if "created_at" in cols:
        select_cols.append("COALESCE(created_at, datetime('now')) AS created_at")
    else:
        select_cols.append("datetime('now') AS created_at")

    # If id exists include it, otherwise rowid will be used
    if "id" in cols:
        insert_sql = f"INSERT OR IGNORE INTO users_new (id, username, password, created_at) SELECT id, {', '.join(select_cols)} FROM users"
    else:
        insert_sql = f"INSERT OR IGNORE INTO users_new (username, password, created_at) SELECT {', '.join(select_cols)} FROM users"

    cur.execute(insert_sql)
    conn.commit()

    # Drop old table and rename new
    cur.execute("DROP TABLE users")
    cur.execute("ALTER TABLE users_new RENAME TO users")
    conn.commit()
    conn.close()


def save_feedback(username: str, feedback: str):
    """Save feedback into the SQLite `feedback` table in `app.db`.
    Formerly feedback was appended to `data/users.csv`. This function now
    inserts feedback into the DB. Existing feedback rows in the CSV (if any)
    are migrated to the DB by `ensure_feedback_table()`.
    """
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Try to resolve user id for stronger linkage
        user_id = None
        if username:
            cur.execute("SELECT id FROM users WHERE username=?", (username,))
            r = cur.fetchone()
            if r:
                user_id = r[0]

        # Ensure feedback table has user_id column (migration may have added it)
        cur.execute("PRAGMA table_info(feedback)")
        cols = [c[1] for c in cur.fetchall()]
        if 'user_id' in cols:
            cur.execute(
                "INSERT INTO feedback (username, user_id, feedback, feedback_at) VALUES (?,?,?,?)",
                (username or '', user_id, feedback, ts),
            )
        else:
            cur.execute(
                "INSERT INTO feedback (username, feedback, feedback_at) VALUES (?,?,?)",
                (username or '', feedback, ts),
            )
        conn.commit()
        conn.close()
        # Also append to a CSV log for easy access
        try:
            os.makedirs(os.path.dirname(FEEDBACK_LOG_PATH), exist_ok=True)
            df_row = pd.DataFrame([{
                'username': username or '',
                'feedback': feedback,
                'feedback_at': ts
            }])
            if not os.path.exists(FEEDBACK_LOG_PATH):
                df_row.to_csv(FEEDBACK_LOG_PATH, index=False)
            else:
                df_row.to_csv(FEEDBACK_LOG_PATH, mode='a', header=False, index=False)
        except Exception:
            # don't fail the main operation if CSV logging fails
            pass
        return True, "Saved"
    except Exception as e:
        return False, str(e)


def ensure_feedback_table():
    """Ensure a simple feedback table exists and migrate feedback from CSV if present.
    Table schema: id, username, feedback, feedback_at
    """
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            feedback TEXT,
            feedback_at TEXT
        )
        """
    )
    conn.commit()

    # Ensure user_id column exists for stronger linkage
    cur.execute("PRAGMA table_info(feedback)")
    existing = [c[1] for c in cur.fetchall()]
    if 'user_id' not in existing:
        try:
            cur.execute("ALTER TABLE feedback ADD COLUMN user_id INTEGER")
            conn.commit()
            # refresh existing list
            cur.execute("PRAGMA table_info(feedback)")
            existing = [c[1] for c in cur.fetchall()]
        except Exception:
            # SQLite may fail if certain constraints exist; ignore and continue
            pass

    # Migrate any existing feedback stored in the CSV file (older behavior)
    try:
        if os.path.exists(USERS_CSV_PATH):
            df = pd.read_csv(USERS_CSV_PATH)
            if 'feedback' in df.columns:
                migrated = 0
                for _, row in df.iterrows():
                    fb = row.get('feedback', '')
                    if pd.notna(fb) and str(fb).strip() != '':
                        uname = row.get('username', '') if 'username' in df.columns else ''
                        fb_at = row.get('feedback_at', '') if 'feedback_at' in df.columns else ''
                        if not fb_at or pd.isna(fb_at):
                            fb_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        cur.execute(
                            "SELECT id FROM feedback WHERE username=? AND feedback=? AND feedback_at=?",
                            (uname, fb, fb_at),
                        )
                        if not cur.fetchone():
                            # attempt to resolve user id
                            uid = None
                            if 'username' in df.columns and uname:
                                cur.execute("SELECT id FROM users WHERE username=?", (uname,))
                                r = cur.fetchone()
                                if r:
                                    uid = r[0]
                            if 'user_id' in existing:
                                cur.execute(
                                    "INSERT INTO feedback (username, user_id, feedback, feedback_at) VALUES (?,?,?,?)",
                                    (uname or '', uid, fb, fb_at),
                                )
                            else:
                                cur.execute(
                                    "INSERT INTO feedback (username, feedback, feedback_at) VALUES (?,?,?)",
                                    (uname or '', fb, fb_at),
                                )
                            migrated += 1
                if migrated > 0:
                    conn.commit()
            # Archive the CSV so migration doesn't run repeatedly
            try:
                bak = USERS_CSV_PATH + ".migrated"
                shutil.move(USERS_CSV_PATH, bak)
            except Exception:
                pass
    except Exception:
        # migration is best-effort; don't block app startup on failure
        pass

    conn.close()

 # ---------- Authentication Helpers ----------
def current_user():
    if os.path.exists(CURRENT_USER_FILE):
        with open(CURRENT_USER_FILE, "r") as f:
            username = f.read().strip()
            if username:
                # verify exists in DB
                conn = get_db_conn()
                cur = conn.cursor()
                cur.execute("SELECT id, username FROM users WHERE username=?", (username,))
                row = cur.fetchone()
                conn.close()
                if row:
                    return {"id": row[0], "username": row[1]}
    return None

def set_current_user(username):
    with open(CURRENT_USER_FILE, "w") as f:
        f.write(username)

def clear_current_user():
    if os.path.exists(CURRENT_USER_FILE):
        os.remove(CURRENT_USER_FILE)

def signup(username, password):
    conn = get_db_conn()
    cur = conn.cursor()
    try:
        # prevent duplicate username
        cur.execute("SELECT id FROM users WHERE username=?", (username,))
        if cur.fetchone():
            conn.close()
            return False, "Username already exists"

        created_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        hashed = hash_password(password)
        cur.execute("INSERT INTO users (username, password, created_at) VALUES (?,?,?)", (username, hashed, created_at))
        conn.commit()
        conn.close()
        # Append to users export CSV (store hashed password for safety)
        try:
            os.makedirs(os.path.dirname(USERS_EXPORT_PATH), exist_ok=True)
            df_row = pd.DataFrame([{
                'username': username,
                'created_at': created_at,
                'password': hashed
            }])
            if not os.path.exists(USERS_EXPORT_PATH):
                df_row.to_csv(USERS_EXPORT_PATH, index=False)
            else:
                df_row.to_csv(USERS_EXPORT_PATH, mode='a', header=False, index=False)
        except Exception:
            pass
        return True, "Account created"
    except Exception as e:
        conn.close()
        return False, str(e)

def check_credentials(username, password):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, password FROM users WHERE username=?", (username,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False

    user_id, stored = row[0], row[1]

    def log_login(u):
        try:
            os.makedirs(os.path.dirname(USER_ACTIVITY_PATH), exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            df_row = pd.DataFrame([{
                'username': u,
                'event': 'login',
                'at': ts
            }])
            if not os.path.exists(USER_ACTIVITY_PATH):
                df_row.to_csv(USER_ACTIVITY_PATH, index=False)
            else:
                df_row.to_csv(USER_ACTIVITY_PATH, mode='a', header=False, index=False)
        except Exception:
            pass

    # If stored password looks like a sha256 hex (64 chars) we verify normally
    if stored and isinstance(stored, str) and len(stored) == 64 and all(c in '0123456789abcdef' for c in stored.lower()):
        ok = verify_password(password, stored)
        if ok:
            log_login(username)
        conn.close()
        return ok

    # Otherwise assume legacy plaintext password: check directly and migrate on success
    if stored == password:
        try:
            hashed = hash_password(password)
            cur.execute("UPDATE users SET password=? WHERE id=?", (hashed, user_id))
            conn.commit()
        except Exception:
            pass
        log_login(username)
        conn.close()
        return True

    conn.close()
    return False


def hash_password(password: str) -> str:
    # use sha256 for now (better to use bcrypt in production)
    return hashlib.sha256(password.encode('utf-8')).hexdigest()


def verify_password(password: str, hashed: str) -> bool:
    return hashlib.sha256(password.encode('utf-8')).hexdigest() == hashed

# ---------- UI Pages ----------
 # ---------- Login Page ----------
def show_login():
    st.header("🔐 Login to open project")
    uname = st.text_input("Username", key="login_user")
    pwd = st.text_input("Password", type='password', key="login_pass")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Login"):
            if check_credentials(uname, pwd):
                set_current_user(uname)
                st.success("Logged in — opening project...")
                st.rerun()
            else:
                st.error("Invalid credentials")
    with col2:
        if st.button("Go to Signup"):
            st.session_state['mode'] = 'signup'
            st.rerun()

 # ---------- Signup Page ----------
def show_signup():
    st.header("📝 Signup (creates account & logs you in)")
    uname = st.text_input("Choose username", key="signup_user")
    pwd = st.text_input("Choose password", type='password', key="signup_pass")
    if st.button("Create account"):
        if not uname or not pwd:
            st.error("Enter both username and password")
        else:
            ok, msg = signup(uname, pwd)
            if ok:
                set_current_user(uname)
                st.success("Account created and logged in. Opening project...")
                st.rerun()
            else:
                st.error("Signup failed: " + msg)
    if st.button("Back to Login"):
        st.session_state['mode'] = 'login'
        st.rerun()

 # ---------- Home Page ----------
def page_home(user):
    st.title("Data Analyzer app")
    st.write(f"Welcome, **{user['username']}**")
    st.write("Upload CSV or Excel files (xls/xlsx) and then go to Data Analysis or Charts to explore them.")

    st.subheader("Upload your dataset(s)")
    uploaded_files = st.file_uploader(
        "Upload CSV or Excel files (you can upload multiple)",
        type=["csv", "xls", "xlsx"],
        accept_multiple_files=True
    )

    if 'uploaded_dfs' not in st.session_state:
        st.session_state['uploaded_dfs'] = {}

    if uploaded_files:
        for up in uploaded_files:
            name = up.name
            try:
                ext = os.path.splitext(name)[1].lower()
                if ext == '.csv':
                    df = pd.read_csv(up)
                else:
                    up.seek(0)
                    df = pd.read_excel(up)
                st.session_state['uploaded_dfs'][name] = df
                st.success(f"Loaded {name} — {df.shape[0]} rows, {df.shape[1]} columns")
            except Exception as e:
                st.error(f"Failed to load {name}: {e}")

    if st.session_state.get('uploaded_dfs'):
        st.subheader("Uploaded files")
        files = list(st.session_state['uploaded_dfs'].keys())
        sel = st.selectbox("Select file to view", files)
        df_sel = st.session_state['uploaded_dfs'][sel]
        st.markdown(f"**{sel}** — {df_sel.shape[0]} rows, {df_sel.shape[1]} columns")
        # show full dataframe (no limit) as requested
        st.dataframe(df_sel)

 # ---------- Data Analysis Page ----------
def page_analysis(user):
    st.title("Data Analysis")
    st.write(f"Showing analysis for **{user['username']}**")
    if not st.session_state.get('uploaded_dfs'):
        st.info("No dataset uploaded yet. Go to Home and upload CSV/Excel files to analyze.")
        return

    files = list(st.session_state['uploaded_dfs'].keys())
    # Use a persistent key for selected dataset so selection survives navigation
    if 'analysis_dataset' not in st.session_state:
        st.session_state['analysis_dataset'] = files[0]
    first_name = st.selectbox("Select dataset to analyze", files, key='analysis_dataset')
    df = st.session_state['uploaded_dfs'][first_name]
    # Show full dataset preview before analysis as requested
    st.subheader(f"Full dataset preview — {first_name} (showing all rows)")
    st.dataframe(df)

    st.header('Now, You can analyze the data here')
    #  Row Selection 
    # Per-dataset persistent widget keys
    rows_key = f"analysis_rows::{first_name}"
    if rows_key not in st.session_state:
        st.session_state[rows_key] = 10
    rows = st.number_input("How many rows do you want to see?", 
                           min_value=5, max_value=len(df), value=st.session_state[rows_key], step=5, key=rows_key)

    #  Search Function 
    search_col_key = f"analysis_searchcol::{first_name}"
    if search_col_key not in st.session_state:
        # default to first column
        st.session_state[search_col_key] = list(df.columns)[0] if len(df.columns) > 0 else ''
    search_col = st.selectbox("Select column to search", df.columns, key=search_col_key)

    search_val_key = f"analysis_searchval::{first_name}"
    if search_val_key not in st.session_state:
        st.session_state[search_val_key] = ''
    search_val = st.text_input("Enter value to search", key=search_val_key)

    filtered_df = df.copy()
    if search_val:
        filtered_df = filtered_df[filtered_df[search_col].astype(str).str.contains(search_val, case=False, na=False)]


    # Data range (row range) selection based on 'rows' value
    st.subheader("Select Data Range (Rows)")
    data_min = 0
    data_max = max(0, len(filtered_df) - int(rows))
    start_key = f"analysis_start::{first_name}"
    if start_key not in st.session_state:
        st.session_state[start_key] = 0
    if data_max > 0:
        data_from = st.slider(
            f"Select start row (showing {int(rows)} rows)",
            min_value=data_min,
            max_value=data_max,
            value=st.session_state[start_key],
            step=1,
            key=start_key
        )
        data_to = data_from + int(rows)
        filtered_range_df = filtered_df.iloc[data_from:data_to]
        st.subheader("Select Columns to Display")
        cols_key = f"analysis_cols::{first_name}"
        if cols_key not in st.session_state:
            st.session_state[cols_key] = list(filtered_range_df.columns)
        display_columns = st.multiselect(
            "Choose columns to display",
            options=list(filtered_range_df.columns),
            default=st.session_state[cols_key],
            key=cols_key
        )
        st.dataframe(filtered_range_df[display_columns])
        # Persist the final dataset shown in Analysis so Charts can use it directly
        try:
            st.session_state['analysis_result_df'] = filtered_range_df[display_columns].copy()
            st.session_state['analysis_result_name'] = f"{first_name} - analyzed"
        except Exception:
            # If copy fails for some reason, still store the object reference
            st.session_state['analysis_result_df'] = filtered_range_df[display_columns]
            st.session_state['analysis_result_name'] = f"{first_name} - analyzed"
        # Save the per-dataset state (for reuse when returning)
        st.session_state.setdefault('analysis_state', {})
        st.session_state['analysis_state'][first_name] = {
            'rows': st.session_state.get(rows_key),
            'search_col': st.session_state.get(search_col_key),
            'search_val': st.session_state.get(search_val_key),
            'start': st.session_state.get(start_key),
            'display_columns': st.session_state.get(cols_key)
        }
    else:
        st.info("No data to display for the selected filters.")
        filtered_range_df = filtered_df.iloc[0:0]
        st.dataframe(filtered_range_df)
    # Download button for filtered dashboard data
    csv = filtered_range_df.to_csv(index=False).encode('utf-8')
    st.download_button(
        label="Download Dashboard Data as CSV",
        data=csv,
        file_name='dashboard_data.csv',
        mime='text/csv',
    )

 # ---------- Charts/Visualization Page ----------
def page_charts(user):
    if not st.session_state.get('uploaded_dfs'):
        st.info("No dataset uploaded yet. Go to Home and upload CSV/Excel files to create charts.")
        return
    files = list(st.session_state['uploaded_dfs'].keys())
    # If there's an analysis result in session, add it as a selectable option at top
    use_analysis_label = None
    if st.session_state.get('analysis_result_df') is not None:
        use_analysis_label = f"Last analysis: {st.session_state.get('analysis_result_name','result')}"
        files = [use_analysis_label] + files

    selected = st.selectbox("Select dataset for charts", files)
    if use_analysis_label and selected == use_analysis_label:
        df = st.session_state['analysis_result_df']
    else:
        df = st.session_state['uploaded_dfs'][selected]

    # Guard: ensure dataframe has columns
    if df is None or df.empty or len(df.columns) == 0:
        st.warning("The selected dataset is empty or has no columns to chart. Go to Data Analysis to prepare a dataset.")
        return

    st.subheader("Custom Chart Builder")
    chart_types = ["Bar","Line","Pie","Scatter","Histogram","Box","Area","Violin","Density Heatmap","Funnel","Sunburst","Treemap","Heatmap"]
    chart_type = st.selectbox("Select chart type", chart_types)

    x_col = st.selectbox("X column", df.columns, key="chart_x")
    y_col = st.selectbox("Y column (optional)", [None] + list(df.columns), key="chart_y")
    color_col = st.selectbox("Color / group (optional)", [None] + list(df.columns), key="chart_color")

    plot_kwargs = {}
    if chart_type == "Pie":
        plot_kwargs['names'] = x_col
        if y_col:
            plot_kwargs['values'] = y_col
    else:
        plot_kwargs['x'] = x_col
        if y_col:
            plot_kwargs['y'] = y_col
        if color_col:
            plot_kwargs['color'] = color_col

    if st.button("Create Chart"):
        try:
            fig = None
            if chart_type == 'Bar':
                fig = px.bar(df, **plot_kwargs)
            elif chart_type == 'Line':
                fig = px.line(df, **plot_kwargs)
            elif chart_type == 'Pie':
                fig = px.pie(df, **plot_kwargs)
            elif chart_type == 'Scatter':
                fig = px.scatter(df, **plot_kwargs)
            elif chart_type == 'Histogram':
                fig = px.histogram(df, **plot_kwargs)
            elif chart_type == 'Box':
                fig = px.box(df, **plot_kwargs)
            elif chart_type == 'Area':
                fig = px.area(df, **plot_kwargs)
            elif chart_type == 'Violin':
                fig = px.violin(df, **plot_kwargs)
            elif chart_type == 'Density Heatmap':
                if y_col:
                    fig = px.density_heatmap(df, x=x_col, y=y_col)
                else:
                    st.error("Density Heatmap requires both X and Y numeric columns")
            elif chart_type == 'Funnel':
                fig = px.funnel(df, **plot_kwargs)
            elif chart_type == 'Sunburst':
                # sunburst requires path or names/parents
                fig = px.sunburst(df, path=[x_col])
            elif chart_type == 'Treemap':
                fig = px.treemap(df, path=[x_col])
            elif chart_type == 'Heatmap':
                if y_col:
                    fig = px.density_heatmap(df, x=x_col, y=y_col)
                else:
                    st.error("Heatmap requires both X and Y columns")

            if fig is not None:
                # Persist the created figure in session_state so widget changes (like changing
                # download format) do not remove the chart from the page.
                st.session_state['last_chart'] = fig
                st.session_state['last_chart_created'] = True
                st.success("Chart created")
        except Exception as e:
            st.error(f"Chart creation failed: {e}")

    # If a chart was created previously (or in this run) show it and provide download controls.
    if st.session_state.get('last_chart') is not None:
        fig = st.session_state['last_chart']
        st.plotly_chart(fig, use_container_width=True)
        st.markdown("**Download options for the last created chart**")
        fmt = st.selectbox("Download format", ["png","jpeg","svg","pdf","html"], index=0, key='last_chart_fmt')
        fname = st.text_input("Filename (without extension)", value=st.session_state.get('last_chart_fname','chart'), key='last_chart_fname')
        download_label = f"Download {fmt.upper()}"
        try:
            if fmt == 'html':
                data = fig.to_html(full_html=True).encode('utf-8')
                mime = 'text/html'
                ext = 'html'
            else:
                data = fig.to_image(format=fmt)
                mime = f'image/{"jpeg" if fmt=="jpeg" else fmt}' if fmt!='pdf' else 'application/pdf'
                ext = fmt
            st.download_button(label=download_label, data=data, file_name=f"{fname}.{ext}", mime=mime)
        except Exception as e:
            st.error(f"Could not generate {fmt} image (is kaleido installed?). Falling back to HTML download. Error: {e}")
            data = fig.to_html(full_html=True).encode('utf-8')
            st.download_button(label="Download HTML fallback", data=data, file_name=f"{fname}.html", mime='text/html')

# ---------- About Page ----------
 # ---------- About Page ----------
def page_about(user):
    st.title("About Data Analyzer app")

    st.markdown("""
    ## What is Data Analyzer app?

    Data Analyzer app is a lightweight, interactive data exploration and visualization tool built with Streamlit, Pandas and Plotly.

    It lets you quickly upload tabular datasets (CSV, XLS, XLSX), preview full data, perform quick filtering and searches, and build visualizations from any uploaded dataset.

    ### Key features
    - Upload multiple datasets (CSV, Excel) from the Home page.
    - Inspect full datasets (no preview limit) and choose which dataset to analyze.
    - Flexible Data Analysis page: search, slice rows, choose columns to display, and download filtered data.
    - Custom Chart Builder: create Bar, Line, Pie, Scatter, Histogram, Box, Area, Violin, Heatmap, Sunburst, Treemap, Funnel and other charts based on your dataset.
    - Download generated charts in multiple formats (PNG, JPEG, SVG, PDF, or HTML). Note: image export requires the `kaleido` package for some formats.
    - Simple authentication and user store (local SQLite) for basic access control.

    ### How to use
    1. Go to the Home page and upload one or more CSV/XLS/XLSX files.
    2. Visit Data Analysis to select a dataset and preview/filter it.
    3. Go to Charts Analysis to select the dataset, pick chart type and columns, then generate and (optionally) download the chart.

    ### Limitations & notes
    - Uploaded files are stored in the current Streamlit session (in-memory). Restarting the app clears uploads unless we add persistence to disk.
    - For exporting charts to image formats (PNG/JPEG/SVG/PDF) install `kaleido` (pip install kaleido). If `kaleido` is missing, an HTML fallback download is provided.

    """)

    st.markdown("---")

    st.header("Feedback")
    st.write("We'd love to hear from you. Leave feedback below and it will be saved to the local users CSV with a timestamp.")

    # Callback to handle feedback submission. Using on_click lets us modify session_state
    # and then call st.experimental_rerun() to avoid modifying widget-backed keys after instantiation.
    def _submit_feedback():
        fb = st.session_state.get('about_feedback', '').strip()
        if not fb:
            st.session_state['feedback_msg'] = (False, 'Please enter some feedback before submitting.')
            return

        # Use the logged-in username when available
        cu = current_user()
        if cu:
            uname = cu['username']
        else:
            uname = st.session_state.get('feedback_username', '').strip()

        ok, msg = save_feedback(uname, fb)
        st.session_state['feedback_msg'] = (ok, msg)
        if ok:
            # Clear the feedback textarea; keep username if logged-in
            st.session_state['about_feedback'] = ''

    fb_col1, fb_col2 = st.columns([3,1])
    with fb_col1:
        st.text_area("Your feedback", height=120, key="about_feedback")
    with fb_col2:
        cu = current_user()
        if cu:
            # Show logged-in username but make it read-only
            st.text_input("Logged in as", value=cu['username'], disabled=True, key='feedback_username_display')
        else:
            st.text_input("Your name (optional)", key="feedback_username")
        st.button("Submit Feedback", on_click=_submit_feedback)

    # Show result message if present, with extra debug info if failed
    if 'feedback_msg' in st.session_state:
        ok, msg = st.session_state.get('feedback_msg', (False, ''))
        if ok:
            st.success(msg if msg != 'Saved' else 'Thank you — your feedback was saved.')
        else:
            st.error(f"Feedback not saved: {msg}")
    # Footer with developer name and social links (replace placeholders with real URLs)
    dev_name = "Aditya Kumar"
    github_url = "https://github.com/Aditya-kumar-Devloper?tab=overview&from=2025-07-01&to=2025-07-31"
    linkedin_url = "https://www.linkedin.com/in/aditya-kumar-a779a32a2?utm_source=share&utm_campaign=share_via&utm_content=profile&utm_medium=android_app"

    st.markdown(f"**Developer:** {dev_name}")
    st.markdown(
        f"[🐙 GitHub]({github_url})  &nbsp;&nbsp; [🔗 LinkedIn]({linkedin_url})",
        unsafe_allow_html=True,
    )


def page_admin(user):
    # Only allow admin user (username 'admin') to access this page for now
    if user['username'] != 'admin':
        st.error('Admin access required')
        return

    st.title('Admin — Feedback')
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, username, user_id, feedback, feedback_at FROM feedback ORDER BY id DESC LIMIT 200")
    rows = cur.fetchall()
    conn.close()

    if not rows:
        st.info('No feedback entries found')
        return

    df = pd.DataFrame(rows, columns=['id','username','user_id','feedback','feedback_at'])
    st.dataframe(df)

    # Export feedback to CSV
    try:
        csv_data = df.to_csv(index=False).encode('utf-8')
        st.download_button('Download feedback as CSV', data=csv_data, file_name='feedback_export.csv', mime='text/csv')
    except Exception:
        pass

    to_delete = st.multiselect('Select feedback ids to delete', options=list(df['id']))
    if st.button('Delete selected') and to_delete:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.executemany('DELETE FROM feedback WHERE id=?', [(i,) for i in to_delete])
        conn.commit()
        conn.close()
        st.success('Deleted selected entries — refresh the page')

# ---------- Main ----------
 # ---------- Main App Logic ----------
def main():
    st.set_page_config(page_title="Data Analyzer app", layout="wide")
    # initialize session mode
    if 'mode' not in st.session_state:
        st.session_state['mode'] = 'login'
    # ensure data directory exists
    try:
        os.makedirs(os.path.dirname(DB_PATH) or 'data', exist_ok=True)
    except Exception:
        pass
    # Ensure DB schema is up-to-date
    ensure_users_table()
    ensure_feedback_table()
    user = current_user()
    if not user:
        # show only auth pages
        if st.session_state['mode'] == 'signup':
            show_signup()
        else:
            show_login()
    else:
        # show original project with sidebar navigation
        st.sidebar.markdown(f"**Logged in:** {user['username']}")
        if st.sidebar.button("Logout"):
            clear_current_user()
            st.session_state['mode'] = 'login'
            st.rerun()

        st.sidebar.title("Navigation")
        nav_items = ["Home","Data Analysis","Charts Analysis","About"]
        if user['username'] == 'admin':
            nav_items.insert(3, 'Admin')
        page = st.sidebar.radio("Go to", nav_items)
        if page == "Home":
            page_home(user)
        elif page == "Data Analysis":
            page_analysis(user)
        elif page == "Charts Analysis":
            page_charts(user)
        elif page == "Admin":
            page_admin(user)
        elif page == "About":
            page_about(user)

if __name__ == '__main__':
    main()