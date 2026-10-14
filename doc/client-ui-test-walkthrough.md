# Full UI test walkthrough — ESG Workspace Chatbot

*A guided, click-by-click test for business reviewers. No technical knowledge
needed. Work through it at your own pace and note anything that feels confusing,
missing, or wrong — your feedback shapes the next iteration.*

---

## 1. What you are testing

A document question-and-answer assistant. Each company gets its own private
**workspaces** (for example *Finance* or *HR*), fills them with its own
documents, and its staff ask questions in plain language. The assistant answers
**only** from those documents and shows the exact source it used — the customer's
document stays private to that customer.

The system is sold **per company** (per *organisation*): an organisation buys a
plan that includes a number of workspaces and user seats, and its employees share
it. There is also a free trial tier for individuals, limited to a fixed number of
questions.

---

## 2. What is ready to review (current work)

Everything listed below is implemented and can be exercised through the browser:

| Area | What you can check |
| --- | --- |
| **Private workspaces** | Each workspace has its own documents and answers; a Finance user can never see HR content. |
| **Answers with sources** | Every answer lists the documents it used; clicking one opens the real file (PDFs open at the right page). |
| **Conversations** | Follow-up questions keep their context; past sessions are saved and can be reopened. |
| **Search** | A "search only" mode returns matching passages without generating an answer. |
| **Voice** | Ask by speaking; answers can be read aloud. |
| **Languages** | English, Simplified Chinese and Hindi — ask or answer in your language. |
| **Organisations & seats** | An organisation owns the workspaces and its people; a manager adds seats, sets access, and manages the team. |
| **Plans & billing** | Plan catalogue (Team / Business / Enterprise) with a Stripe payment path. |
| **Free tier** | A visitor can self-register, confirm by email, try a capped number of questions, then be invited to upgrade. |
| **Admin console** | The platform team curates the document library, creates organisations, assigns documents, and sees usage. |
| **Analytics** | Question, token and cost reporting, per organisation and per workspace. |

---

## 3. Before you start

Your administrator will send you:

- **The web address** of the test site: `https://__________________`
- **The admin access key**: `<sent separately — do not share>` (used only on the
  platform admin screen)

You will use three different sign-in areas, all on the same web address:

| Screen | Who it's for | Address |
| --- | --- | --- |
| Chat app | End users (employees, free visitors) | `/ui` |
| Organisation console | A customer's manager (owner/admin) | `/owner` |
| Platform admin console | Your own team (setup & oversight) | `/admin` |

> **Note on email:** this test environment does **not** yet send real email, so
> confirmation links appear in the admin console's **Outbox** card. Your
> administrator can also mark an account confirmed by hand. See Step 4c.

---

## 4. The test, part by part

### Part A — Platform administrator (your team)

This is the setup a platform operator does for each new customer.

**A1. Sign in to the admin console.**
1. Open `/admin`.
2. In the **Admin access** card, paste the access key into the **API key** field
   and click **Save**. You should see a **Connected** badge.

**A2. Add documents to the master library.**
1. Go to the **Organisations** tab. The top card is **Master library**.
2. Drag a PDF/Word/Excel file onto the drop zone, give it a **Subject** (e.g.
   `climate-disclosure`), and click **Add to library**.
3. The file appears in the library table marked **not assigned** — meaning no
   customer can see it yet. *(This is deliberate: nothing is shared until you
   assign it on purpose.)*

**A3. Create a customer organisation.**
1. Still on **Organisations**, in the **Organisations** card enter an id
   (e.g. `acme-ltd`) and a display name (e.g. *Acme Ltd*).
2. Click **Create**. The organisation now appears in the list.

**A4 (optional). Create a workspace.**
- Go to the **Workspaces** tab → **Create workspace**. Give it a category name,
  a label, an emoji, and a one-line description (this is what users see at
  sign-up). Click **Create**. *(Built-in Finance and HR workspaces already
  exist, so this step is only needed for a new one.)*

**A5. Assign documents to the organisation's workspace.**
1. Back on **Organisations**, in the organisation's row click **Content**.
2. Pick the workspace in the dropdown.
3. Under **Assign from the master library**, tick a document, then click
   **Assign to <workspace>**. The document now appears under **Served in
   <workspace>**.

**A6 (optional). Upload a document straight into a workspace.**
- In the same **Content** area, use **Upload to this workspace**. The file is
  added to the master library and assigned to this organisation only.

**A7. Create a user inside the organisation.**
1. Go to the **Users** tab → **Create user**.
2. Fill in **Name**, **Email**, **Password** (or click **Generate**), choose the
   **Category** (workspace), and set **Organisation** to the customer you
   created.
3. Click **Create**.

**A8 (optional). Invite someone instead.**
1. **Workspaces** tab → **Invite links** card.
2. Choose the **Category** (workspace) and **Organisation**, set **Uses allowed**
   and **Expires in (h)**, then click **Create invite**. Copy the one-time link
   and send it. Anyone opening it joins that organisation's workspace.

**A9 (optional). Move a free visitor into the customer.**
1. **Organisations** tab → the organisation's **People in <name>** card →
   **Move a free account here…**
2. Enter the free account's email and the workspace(s) to grant. They keep their
   account and history, and lose access to anything not listed.

**A10. Manage people and ownership.**
- In **People in <organisation>**: change a person's role (Member / Admin /
  Owner), tick the workspaces they can use, **Revoke** a role, or **Transfer
  ownership** (one owner per organisation).

**A11. Check usage, audit and the free-account cap.**
- **Usage & Cost** tab — question, token and estimated-cost figures; try the
  date range and **⬇ Export CSV**.
- **Audit** tab — a record of important actions.
- **Users** tab → **Free accounts** shows how full the free tier is.

---

### Part B — Organisation owner (the customer's manager)

Sign in to the chat app as the owner account (created in Part A), then open the
organisation console.

**B1. Open `/owner`** (or click **Organisation console** in the chat header).
You'll see four tabs: **Overview**, **Analytics**, **People**, **Billing**.

**B2. Overview** — a dashboard: workspaces, members, indexed content, and
questions used this period against the plan's allowance. Each workspace has an
**Open chat** button.

**B3. People → Add a seat.**
1. **People** tab → **Add a seat**.
2. Enter the person's **Email**, **Full name**, a **Password**, and their
   **Primary workspace**, then click **Add member**.
3. The new member is created already-confirmed and belongs to your organisation.

**B4. People → manage the team.**
- Change a member's role (**member** / **admin**).
- Use **Reset password**, **Suspend** / **Reactivate**, or **Remove**.
- Create shareable **invite links** under **Invite links (shown once)**.

**B5. Billing.**
- The **Billing** tab shows the **Current plan**, the **question pool** meter,
  and **seats by workspace**.
- **Choose a plan** / **Manage / change plan** starts a hosted payment page
  (Stripe). *This may be in test mode or switched off in this environment — note
  whether you can reach it and how it reads.*

**B6. Analytics** — tokens and estimated cost, by period, workspace, and call
type.

---

### Part C — End user (an employee / seat)

**C1. Sign in at `/ui`** with the account your administrator or manager created.
(Or use **Create account** if you were given an invite link.)

**C2. Ask a question.**
- Type a question about the workspace's documents in **"Ask about your workspace
  documents…"** and click **Send**.
- Try a greeting first ("good morning") — it answers instantly with no cost.

**C3. Check the source.**
- Under the answer, the **Sources:** line lists the document(s) used. Click one —
  the real file opens (a PDF should jump to the cited page).

**C4. Ask a follow-up** ("and which of those is most material?") to check it
keeps the thread's context.

**C5. Sessions.**
- The left **Sessions** list keeps your past conversations; click one to reopen,
  or **×** to delete. **New session** starts fresh.

**C6. Search (no LLM).**
- In the **Search (no LLM)** box, type a phrase and click **Go** — you get
  matching passages only, no generated answer.

**C7. Voice (optional).**
- Click **🎤** to record, click again to stop; the transcript lands in the box
  for you to check before sending. Use **🔊** to have answers read aloud.

**C8. Language.**
- Use the **English / 中文 / हिन्दी** selector, or simply type a question in
  Chinese or Hindi — the answer comes back in that language.

**C9. Workspace switcher.**
- If you belong to more than one workspace, a switcher appears in the header;
  changing it changes which documents the assistant uses.

---

### Part D — Free tier (a first-time visitor)

**D1. Sign up.**
1. Open `/ui` → **Create account**.
2. Enter **Full name**, **Email**, choose a **Workspace (category)**, set a
   **Password** (8+ characters), click **Create account**.

**D2. Confirm your email.**
- The app shows **"Confirm your email address"** and the composer stays locked
  until confirmed.
- **No real email in this test environment:** your administrator opens the
  **Outbox** card in the admin console and uses **Open link**, or marks the
  account confirmed under **Users → Pending email confirmations**. (The new
  account can also click **Resend email**, which reveals the link.)

**D3. Use the free allowance.**
- After confirming, ask questions. A badge shows **"Free plan · N questions
  left"**.

**D4. Reach the cap.**
- When the free allowance runs out, the next question is politely refused and an
  **Upgrade now / Upgrade** card appears offering the paid plans.

---

## 5. What "good" looks like — quick checklist

| # | Check | Pass? |
| --- | --- | --- |
| 1 | Admin can add a document to the master library, and it serves nobody until assigned | ☐ |
| 2 | Admin can create an organisation and assign a document to one of its workspaces | ☐ |
| 3 | An invited/created user lands inside the correct organisation and workspace | ☐ |
| 4 | A user gets answers **with sources**, and the source file opens | ☐ |
| 5 | Follow-up questions keep context; past sessions reopen | ☐ |
| 6 | Search returns passages with no generated answer | ☐ |
| 7 | A Finance user cannot retrieve HR content (and vice versa) | ☐ |
| 8 | Voice records, transcribes for review, and can read answers aloud | ☐ |
| 9 | Chinese and Hindi questions are answered in-language | ☐ |
| 10 | The organisation console shows overview, people, analytics and plan | ☐ |
| 11 | A free visitor can sign up, confirm, and is capped, then prompted to upgrade | ☐ |
| 12 | The admin console shows usage/cost and an audit trail | ☐ |

---

## 6. Your feedback

Please tell us, in your own words:

1. **Clarity** — Was each screen obvious? Where did you hesitate or have to
   guess what to click?
2. **Value** — Which feature feels most useful to your customers? Which feels
   like a distraction?
3. **Seats & plans** — Do the Team / Business / Enterprise packages and the
   per-workspace pricing match how you'd sell this?
4. **Free tier** — Is the free allowance (default 50 questions) about right —
   too generous, or too tight?
5. **Documents** — Is assigning documents from a central library the right
   process for how your customers will actually work?
6. **Gaps** — Anything you expected to find and did not?
7. **Wording & branding** — Any labels or copy you'd change for your market?
8. **Priority** — If we do one thing next, what should it be?

---

## 7. Scope notes (what is deliberately not in this round)

So expectations are clear, these are known gaps and are not defects:

- **No real email yet** — confirmation and password-reset messages land in the
  admin **Outbox** until a mail relay is configured.
- **Billing may be in test mode** (or switched off) in this environment; the live
  payment flow is configured at go-live.
- **Customers do not upload their own documents** — a platform administrator
  assigns them. This is a deliberate trust/privacy boundary, open for discussion.
- **One instance only** — the app is designed to run as a single copy for now;
  scaling to many copies is a later infrastructure step.

---

## 7b. Plain-English glossary

- **Workspace** — a private folder of documents with its own assistant (e.g.
  *Finance*, *HR*).
- **Organisation** — a customer company; owns workspaces, people and billing.
- **Seat** — one user's place in an organisation.
- **Free tier** — a self-service trial account with a fixed question allowance.
- **Master library** — the platform team's curated document originals, assigned
  to customers on purpose.
- **Citation / source** — the document an answer came from.
- **Session** — one saved conversation.
