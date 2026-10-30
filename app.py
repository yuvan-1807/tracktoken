"""Hospital Token Tracker. Run with:  streamlit run app.py

Roles:
  patient  takes a token, sees live position, gets SMS when called
  staff    runs the queue: call, hold, re-inject, emergency pause, walk-ins
  admin    everything staff can do, plus accounts, SMS tools, analytics
Waiting room board needs no login.
"""
import os
import sqlite3

import pandas as pd
import streamlit as st

from auth import AuthStore
from config import get_setting
from queue_engine import CALLED, DONE, EMERGENCY_DELAY_MIN, HELD, WAITING, QueueEngine
from sms import SmsSender

st.set_page_config(page_title="Hospital Token Tracker", page_icon="🏥", layout="wide")

DB_PATH = os.getenv("TOKEN_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "hospital.db"))
STATUS_LABEL = {CALLED: "🔵 Now", WAITING: "⚪ Waiting", HELD: "🟠 Absent", DONE: "🟢 Done"}


@st.cache_resource
def services():
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    sms = SmsSender(db)
    auth = AuthStore(db)
    eng = QueueEngine(db, sms)
    if eng.stats()["total"] == 0:  # demo queue so the screens are not empty
        for name in ["Walk-in A", "Walk-in B", "Walk-in C"]:
            eng.take_token(name)
        eng.call_next()
    return eng, auth, sms


def sign_out():
    st.session_state.pop("user", None)
    st.session_state.nav = "Patient sign in"
    st.rerun()


# ---------------- navigation helper ----------------
def go_to(page_name):
    st.session_state.nav = page_name


# ---------------- patient sign in (separate from staff) ----------------
def page_patient_login(auth):
    st.title("Patient sign in")
    st.caption("Sign in to get a token and track your turn.")
    tab_in, tab_up = st.tabs(["Sign in", "Create account"])

    with tab_in:
        with st.form("patient_login"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            if st.form_submit_button("Sign in", type="primary"):
                user = auth.authenticate(username, password)
                if user is None:
                    st.error("Wrong username or password.")
                elif user["role"] != "patient":
                    st.error("This is a staff or admin account. Use Staff & admin login instead.")
                else:
                    st.session_state.user = user
                    st.rerun()

    with tab_up:
        with st.form("signup"):
            name = st.text_input("Full name")
            phone = st.text_input("Mobile number (for SMS alerts)", placeholder="10-digit number")
            username = st.text_input("Choose a username")
            password = st.text_input("Choose a password (6+ characters)", type="password")
            if st.form_submit_button("Create account", type="primary"):
                try:
                    auth.create(username, password, name, "patient", phone)
                    st.success("Account created. Go to the Sign in tab to sign in.")
                except ValueError as exc:
                    st.error(str(exc))

    st.divider()
    st.markdown("Clinic staff or administrator?")
    st.button("Click here for staff and admin sign in", on_click=go_to, args=("Staff & admin login",))


# ---------------- staff and admin sign in (separate screen) ----------------
def page_staff_login(auth):
    st.title("Staff and admin sign in")
    st.caption("For clinic staff and administrators only.")
    with st.form("staff_login"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Sign in", type="primary"):
            user = auth.authenticate(username, password)
            if user is None:
                st.error("Wrong username or password.")
            elif user["role"] == "patient":
                st.error("This is a patient account. Use Patient sign in instead.")
            else:
                st.session_state.user = user
                st.rerun()
    if not auth.has_role("admin"):
        st.info("No admin account exists yet. Set ADMIN_USERNAME and ADMIN_PASSWORD in your secrets, then restart the app.")
    st.button("Back to patient sign in", on_click=go_to, args=("Patient sign in",))


# ---------------- patient ----------------
@st.fragment(run_every=3)
def my_status(eng, token):
    pos = eng.position(token)
    if pos is None:
        st.error("Token not found.")
        return
    if eng.emergency:
        st.error(
            f"🚨 The doctor is attending a critical emergency. Appointments are paused. "
            f"We will update you within {EMERGENCY_DELAY_MIN} minutes."
        )
    if pos["status"] == CALLED:
        st.success("🔔 **It's your turn.** Please go to the counter now.")
    elif pos["status"] == WAITING:
        c1, c2, c3 = st.columns(3)
        c1.metric("Your token", f"#{pos['token']}")
        c2.metric("Patients ahead", pos["ahead"])
        c3.metric("Estimated wait", f"{pos['eta_min']} min")
        st.caption("Estimates update automatically. You can step out. We'll text you when you're next.")
    elif pos["status"] == HELD:
        st.warning("You were marked absent. Please see the front desk to rejoin the queue.")


def page_my_token(eng, user):
    st.title(f"Hello, {user['full_name']}")
    active = eng.my_token(user["id"])
    if active is None:
        with st.container(border=True):
            st.subheader("Take a token")
            phone = st.text_input("Mobile number for SMS alerts", value=user["phone"] or "", key="tok_phone")
            if st.button("Get my token", type="primary"):
                try:
                    eng.take_token(user["full_name"], phone, user_id=user["id"])
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
    else:
        with st.container(border=True):
            st.subheader(f"Your token is #{active['token']}")
            my_status(eng, active["token"])


# ---------------- staff console ----------------
@st.fragment(run_every=3)
def staff_live(eng, sms):
    """Refreshes every 3 seconds so new tokens appear without a click."""
    s = eng.stats()
    st.caption(f"Live. Updates every few seconds. SMS: {sms.mode_label()}.")

    with st.container(border=True):
        c = st.columns(5)
        c[0].metric("Now serving", f"#{s['serving']}" if s["serving"] else "—")
        c[1].metric("Waiting", s["waiting"])
        c[2].metric("Absent (held)", s["held"])
        c[3].metric("Avg visit", f"{s['avg_service_min']} min")
        c[4].metric("Queue", "Paused 🚨" if eng.emergency else "Running ✅")

    with st.container(border=True):
        st.markdown("**Actions**")
        b = st.columns(4)
        if b[0].button("▶ Call next", type="primary", disabled=eng.emergency):
            _, msg = eng.call_next()
            st.toast(msg)
            st.rerun()
        if b[1].button("⏸ Skip / hold", disabled=s["serving"] is None):
            eng.skip_current()
            st.toast("Moved to the hold list")
            st.rerun()
        if b[2].button("✔ Visit done", disabled=s["serving"] is None):
            eng.mark_done()
            st.rerun()
        if eng.emergency:
            if b[3].button("✅ Resume queue", type="primary"):
                eng.set_emergency(False)
                st.rerun()
        elif b[3].button("🚨 Emergency pause"):
            eng.set_emergency(True)
            st.rerun()

    left, right = st.columns([3, 2])
    with left, st.container(border=True):
        st.markdown("**Live queue**")
        rows = [
            {"Token": f"#{r['token']}", "Name": r["name"], "Status": STATUS_LABEL[r["status"]], "Holds": r["holds"]}
            for r in eng.board()
        ]
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True)
        else:
            st.info("No one in the queue yet.")

    with right, st.container(border=True):
        st.markdown("**Hold list**")
        held = eng.held()
        if not held:
            st.caption("No absent patients.")
        for r in held:
            hc1, hc2 = st.columns([3, 2])
            hc1.write(f"#{r['token']} {r['name']}")
            if hc2.button("Re-inject", key=f"re{r['token']}"):
                eng.reinject(r["token"])
                st.rerun()


def page_staff(eng, sms):
    st.title("Staff console")
    staff_live(eng, sms)

    with st.container(border=True):
        st.markdown("**Add walk-in**")
        with st.form("walkin", border=False):
            wname = st.text_input("Name")
            wphone = st.text_input("Mobile (optional)")
            if st.form_submit_button("Issue token"):
                if not wname.strip():
                    st.error("Enter the patient's name.")
                else:
                    t = eng.take_token(wname, wphone)
                    st.toast(f"Issued token #{t}")

    with st.expander("📜 Activity log"):
        for e in eng.events(20):
            st.text(e["msg"])


# ---------------- admin ----------------
def page_admin(eng, auth, sms, user):
    st.title("Admin")
    tab_acc, tab_sms, tab_log = st.tabs(["Accounts", "SMS", "Activity"])

    with tab_acc:
        with st.container(border=True):
            st.subheader("Create staff or admin account")
            with st.form("new_staff"):
                c1, c2 = st.columns(2)
                name = c1.text_input("Full name")
                username = c2.text_input("Username")
                password = c1.text_input("Temporary password", type="password")
                role = c2.selectbox("Role", ["staff", "admin"])
                if st.form_submit_button("Create account", type="primary"):
                    try:
                        auth.create(username, password, name, role)
                        st.success(f"{role.title()} account '{username}' created.")
                    except ValueError as exc:
                        st.error(str(exc))

        users = auth.list_users()
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Username": u["username"],
                        "Name": u["full_name"],
                        "Role": u["role"],
                        "Phone": u["phone"] or "",
                        "Active": "Yes" if u["active"] else "No",
                    }
                    for u in users
                ]
            ),
            hide_index=True,
        )
        others = [u for u in users if u["id"] != user["id"]]
        if others:
            with st.container(border=True):
                st.markdown("**Turn an account on or off**")
                pick = st.selectbox("Account", [u["username"] for u in others])
                target = next(u for u in others if u["username"] == pick)
                if target["active"]:
                    if st.button(f"Deactivate {pick}"):
                        auth.set_active(target["id"], False)
                        st.rerun()
                elif st.button(f"Reactivate {pick}"):
                    auth.set_active(target["id"], True)
                    st.rerun()

    with tab_sms:
        st.markdown(f"Provider: **{sms.mode_label()}**")
        if not sms.configured():
            st.info(
                "Messages are logged but not sent. To send real SMS, set SMS_PROVIDER=twilio and the "
                "TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER variables, then restart."
            )
        with st.container(border=True):
            st.markdown("**Send a test SMS**")
            with st.form("test_sms"):
                tphone = st.text_input("Mobile number")
                tmsg = st.text_input("Message", value="Test message from Hospital Token Tracker.")
                if st.form_submit_button("Send"):
                    status = sms.send(tphone, tmsg)
                    if status in ("sent", "simulated"):
                        st.success(f"Result: {status}")
                    else:
                        st.error(f"Result: {status}. Check the number and provider settings.")
        with st.container(border=True):
            st.markdown("**Message all waiting patients with a number**")
            with st.form("broadcast"):
                bmsg = st.text_input("Message", value="Clinic update: please check the waiting room board.")
                if st.form_submit_button("Send to waiting patients"):
                    targets = eng.waiting_with_phone()
                    for row in targets:
                        sms.send(row["phone"], bmsg)
                    st.success(f"Sent to {len(targets)} waiting patient(s).")
        counts = sms.counts()
        if counts:
            st.caption("Totals: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        logs = sms.recent(50)
        if logs:
            st.dataframe(
                pd.DataFrame(
                    [
                        {"To": r["to_number"], "Message": r["body"], "Status": r["status"], "Detail": r["detail"]}
                        for r in logs
                    ]
                ),
                hide_index=True,
            )
        else:
            st.caption("No messages yet.")

    with tab_log:
        for e in eng.events(60):
            st.text(e["msg"])


# ---------------- analytics ----------------
def page_analytics(eng, sms):
    st.title("Analytics")
    st.caption("Snapshot of wait times, visit length and no-shows.")
    s = eng.stats()
    with st.container(border=True):
        c = st.columns(5)
        c[0].metric("Tokens issued", s["total"])
        c[1].metric("Avg wait before call", f"{s['avg_wait_min']} min")
        c[2].metric("Avg visit length", f"{s['avg_service_min']} min")
        c[3].metric("No-show rate", f"{s['no_show_pct']} %")
        c[4].metric("SMS logged", sum(sms.counts().values()))
    waits = eng.wait_times()
    with st.container(border=True):
        st.markdown("**Wait before being called, per token (minutes)**")
        if waits:
            st.bar_chart(pd.DataFrame(waits).set_index("token"), color="#0F766E")
        else:
            st.info("No data yet. Call some patients from the staff console.")


# ---------------- waiting room board (public) ----------------
@st.fragment(run_every=3)
def tv_board(eng):
    if eng.emergency:
        st.error(
            f"⚠ **Doctor attending an emergency.** All appointments are temporarily paused. "
            f"Update in {EMERGENCY_DELAY_MIN} minutes."
        )
    cur = eng.current()
    with st.container(border=True):
        st.caption("NOW SERVING")
        st.markdown(f"# {'#' + str(cur['token']) + '  ' + cur['name'] if cur else '—'}")
    board = eng.board()
    waiting = [r for r in board if r["status"] == WAITING]
    held = [r for r in board if r["status"] == HELD]
    left, right = st.columns(2)
    with left, st.container(border=True):
        st.markdown("#### Next up")
        if not waiting:
            st.info("No one is waiting.")
        for i, r in enumerate(waiting[:6]):
            text = f"**#{r['token']}**  {r['name']}"
            if eng.emergency:
                st.error(f"⏸ Paused: {text}")
            elif i == 0:
                st.warning(f"🟡 NEXT: {text}")
            else:
                st.info(f"⚪ {text}")
    with right, st.container(border=True):
        st.markdown("#### Absent: please see the desk")
        if not held:
            st.caption("None")
        for r in held:
            st.error(f"**#{r['token']}**  {r['name']}")


# ---------------- app shell ----------------
eng, auth, sms = services()
# Runs on every page load (not inside the cached startup), so secrets added after the
# first start still create the admin. It does nothing once an admin exists.
auth.ensure_admin(get_setting("ADMIN_USERNAME"), get_setting("ADMIN_PASSWORD"))
user = st.session_state.get("user")

if user is None:
    pages = ["Patient sign in", "Staff & admin login", "Waiting room board"]
else:
    pages = []
    if user["role"] == "patient":
        pages.append("My token")
    if user["role"] in ("staff", "admin"):
        pages.append("Staff console")
    if user["role"] == "admin":
        pages += ["Admin", "Analytics"]
    pages.append("Waiting room board")

if st.session_state.get("nav") not in pages:
    st.session_state.nav = pages[0]

with st.sidebar:
    st.markdown("## 🏥 Token Tracker")
    if user:
        st.markdown(f"Signed in as **{user['full_name']}**  \n*{user['role']}*")
        if st.button("Sign out"):
            sign_out()
    st.divider()
    page = st.radio("Navigate", pages, key="nav", label_visibility="collapsed")
    st.divider()
    if eng.emergency:
        st.error("Queue is paused (emergency)")
    else:
        st.success("Queue is running")
    st.caption(f"SMS: {sms.mode_label()}")

if page == "Patient sign in":
    page_patient_login(auth)
elif page == "Staff & admin login":
    page_staff_login(auth)
elif page == "My token":
    page_my_token(eng, user)
elif page == "Staff console":
    page_staff(eng, sms)
elif page == "Admin":
    page_admin(eng, auth, sms, user)
elif page == "Analytics":
    page_analytics(eng, sms)
else:
    tv_board(eng)
