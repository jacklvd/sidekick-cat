"""Local guided setup for sidekick-cat: GCP project/APIs, the GitHub App (with
the exact permissions it needs), the GitHub Models token, and the Groq key —
ending in a ready-to-copy .env and first-deploy command.

Pure generator only: nothing typed here is sent anywhere or written to disk
except what you explicitly download. Run with:
    uv run --extra setup-wizard streamlit run tools/setup_wizard.py

Env: none. All values come from the form; see .env.example for what they map to.
"""

import sys
from pathlib import Path

import streamlit as st

# Depending on how the script is launched (streamlit CLI vs. AppTest vs. plain
# python), this file's own directory may or may not already be on sys.path —
# add it explicitly so the sibling module always resolves the same way.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from wizard_logic import (  # noqa: E402
    GITHUB_APP_PERMISSIONS,
    GITHUB_APP_WEBHOOK_EVENTS,
    generate_webhook_secret,
    render_deploy_cmd,
    render_enable_apis_cmd,
    render_env_block,
    render_firestore_create_cmd,
    render_teardown_guide,
)

st.set_page_config(page_title="sidekick-cat setup", page_icon="🐱")
st.title("🐱 sidekick-cat setup wizard")
st.caption(
    "Everything you type stays in this browser session — nothing is sent "
    "anywhere or saved to disk except what you download at the end."
)

# --- Sidebar progress tracker. Reads widget values persisted in session_state
# from prior reruns, so the checklist reflects what you've filled in so far.
# These are required for a working .env + deploy; the Models token is the
# optional last-resort fallback provider. ---
_REQUIRED = [
    ("GCP project id", "project"),
    ("GitHub App ID", "app_id"),
    ("Webhook secret", "webhook_secret"),
    ("NVIDIA key", "nvidia_api_key"),
    ("Groq key", "groq_api_key"),
]
with st.sidebar:
    st.header("Setup progress")
    done = [bool(st.session_state.get(k)) for _, k in _REQUIRED]
    st.progress(sum(done) / len(_REQUIRED), text=f"{sum(done)} / {len(done)} required steps")
    for (label, _), ok in zip(_REQUIRED, done):
        st.markdown(f"{'✅' if ok else '⬜️'} {label}")
    st.markdown(
        f"{'✅' if st.session_state.get('models_pat') else '➕'} "
        "Models token _(optional fallback)_"
    )
    st.divider()
    st.caption("Head to the **Summary** tab once the required steps are green.")

tab_overview, tab_gcp, tab_app, tab_nvidia, tab_groq, tab_models, tab_summary, tab_teardown = st.tabs(
    ["1. Overview", "2. GCP", "3. GitHub App", "4. NVIDIA key", "5. Groq key",
     "6. Models token", "7. Summary", "8. Teardown"]
)

with tab_overview:
    st.subheader("What you'll need")
    st.markdown(
        "- A **Google Cloud project** with billing enabled (Cloud Run scales to "
        "zero, so an idle bot costs ~$0)\n"
        "- A **GitHub account** with permission to create a GitHub App on it\n"
        "- A free **[NVIDIA build.nvidia.com](https://build.nvidia.com)** account "
        "for primary review/context inference\n"
        "- A free **[Groq](https://console.groq.com)** account for summaries and fallback\n"
    )
    st.subheader("How this works")
    st.markdown(
        "Work through tabs **2 → 6** — each hands you the exact command, link, or "
        "value for that step. The **Summary** tab then assembles a ready-to-copy "
        "`.env` and your first-deploy `gcloud` command. The sidebar tracks what's "
        "left. Done with the bot later? The **Teardown** tab pauses or removes it."
    )
    st.subheader("What you'll end up with")
    st.markdown(
        "- A branded **GitHub App** installed on your repos\n"
        "- A **Cloud Run service** running the webhook\n"
        "- A filled-in **`.env`** and first-deploy **`gcloud`** command\n"
    )

with tab_gcp:
    st.subheader("GCP project & APIs")
    project = st.text_input(
        "GCP project id", key="project", placeholder="my-sidekick-project",
        help="The project id (not its display name) — lowercase letters, digits, and hyphens.",
    )
    region = st.text_input(
        "Region", value="us-west1", key="region",
        help="Any Cloud Run region. Keep Firestore in the same region for lowest latency.",
    )
    with_firestore = st.checkbox(
        "Share rate-limit caps/dedup across instances with Firestore "
        "(optional — without it, caps are per-instance in-memory)",
        key="with_firestore",
    )
    st.divider()
    if project:
        st.markdown("**1. Enable the APIs the bot needs:**")
        st.code(render_enable_apis_cmd(project), language="bash")
        if with_firestore:
            st.markdown("**2. Create the Firestore database:**")
            st.code(render_firestore_create_cmd(region or "us-west1"), language="bash")
    else:
        st.info("Enter a project id above to generate the commands.")

with tab_app:
    st.subheader("Create the GitHub App")
    st.link_button("Open GitHub → New GitHub App ↗", "https://github.com/settings/apps/new")
    st.markdown(
        "Set these **Repository permissions** exactly — traced from the actual API "
        "calls the bot makes, not guessed:"
    )
    st.table(
        {
            "Permission": [p for p, _, _ in GITHUB_APP_PERMISSIONS],
            "Level": [lvl for _, lvl, _ in GITHUB_APP_PERMISSIONS],
            "Why": [why for _, _, why in GITHUB_APP_PERMISSIONS],
        }
    )
    st.markdown(
        "Subscribe to these **webhook events**: "
        + ", ".join(f"**{e}**" for e in GITHUB_APP_WEBHOOK_EVENTS)
        + ". Install it on **all repositories**."
    )
    st.markdown(
        "After creating the App: note the **App ID**, generate and download a "
        "**private key** (PEM) — that file's contents become `APP_KEY`."
    )
    app_id = st.text_input("App ID", key="app_id", placeholder="123456")

    st.divider()
    st.subheader("Webhook secret")
    st.markdown(
        "A random value the App uses to sign webhook deliveries — paste the same "
        "value into the App's **Webhook secret** field and into `WEBHOOK_SECRET`."
    )
    if st.button("🎲 Generate a webhook secret"):
        st.session_state["webhook_secret"] = generate_webhook_secret()
    if st.session_state.get("webhook_secret"):
        st.code(st.session_state["webhook_secret"], language="text")
        st.caption("Stored for the Summary tab. Generate again to replace it.")

with tab_nvidia:
    st.subheader("NVIDIA key (primary inference for /review and /context)")
    st.link_button("Open build.nvidia.com ↗", "https://build.nvidia.com")
    st.markdown(
        "Sign up (no card) → any model page → **Get API Key**. The key starts "
        "with `nvapi-`. NVIDIA NIM's free tier is rate-limited per request (not "
        "per token), and its models are large-context — so it leads the review "
        "tiers; Groq and GitHub Models stay as fallbacks."
    )
    nvidia_api_key = st.text_input(
        "Paste the key (optional, for the summary below)",
        key="nvidia_api_key", type="password",
    )

with tab_models:
    st.subheader("GitHub Models token (fallback inference)")
    st.link_button(
        "Open GitHub → New fine-grained token ↗",
        "https://github.com/settings/personal-access-tokens/new",
    )
    st.markdown(
        "No repository access needed — under **Account permissions**, set "
        "**Models: Read-only**. This is only the *fallback* provider, so it's optional."
    )
    models_pat = st.text_input(
        "Paste the token (optional, for the summary below)",
        key="models_pat", type="password",
    )

with tab_groq:
    st.subheader("Groq key (summaries + first fallback)")
    st.link_button("Open Groq → API Keys ↗", "https://console.groq.com/keys")
    st.markdown(
        "Sign up (no card) → **API Keys** → create one. It's a single key, no "
        "scopes to set."
    )
    groq_api_key = st.text_input(
        "Paste the key (optional, for the summary below)",
        key="groq_api_key", type="password",
    )

with tab_summary:
    st.subheader("Ready to deploy?")
    missing = [label for label, k in _REQUIRED if not st.session_state.get(k)]
    if missing:
        st.warning("Still needed: " + ", ".join(missing) + ". `APP_KEY` (the PEM) is pasted in by hand.")
    else:
        st.success("All required values are in — copy the `.env` and run the deploy command below.")

    st.divider()
    st.subheader(".env")
    env_text = render_env_block(
        {
            "APP_ID": st.session_state.get("app_id", ""),
            "WEBHOOK_SECRET": st.session_state.get("webhook_secret", ""),
            "NVIDIA_API_KEY": st.session_state.get("nvidia_api_key", ""),
            "GROQ_API_KEY": st.session_state.get("groq_api_key", ""),
            "MODELS_PAT": st.session_state.get("models_pat", ""),
            "with_firestore": st.session_state.get("with_firestore", False),
        }
    )
    st.code(env_text, language="bash")
    st.caption("`APP_KEY` isn't collected here — paste the downloaded PEM's contents in directly.")
    st.download_button("⬇️ Download .env", env_text, file_name=".env", mime="text/plain")

    st.divider()
    st.subheader("First-time deploy command")
    if st.session_state.get("project"):
        st.code(
            render_deploy_cmd(
                st.session_state["project"],
                st.session_state.get("region") or "us-west1",
                st.session_state.get("app_id", ""),
                bool(st.session_state.get("with_firestore")),
            ),
            language="bash",
        )
        st.caption(
            "After it deploys, set the App's webhook URL to the service URL + `/webhook`."
        )
    else:
        st.info("Fill in a GCP project id on the GCP tab to generate this command.")

with tab_teardown:
    st.subheader("Take Sidekick offline")
    st.caption(
        "Pause (reversible) or fully delete everything the bot spun up. Uses the "
        "project id / region from the GCP tab, so the commands are copy-ready."
    )
    st.markdown(
        render_teardown_guide(
            st.session_state.get("project", ""),
            st.session_state.get("region") or "us-west1",
        )
    )
