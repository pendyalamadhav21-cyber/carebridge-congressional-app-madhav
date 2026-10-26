# CareBridge Demo: Speaking Script (Frisco, Texas)

**Video:** `CareBridge-demo-v2.mp4` · **Length:** 3:26 · **No voice-over.** Read this aloud while the video plays.

Timestamps match the video. If you fall behind, skip the lines marked *(optional)*.

---

**0:00 – 0:06 · Title card**
> Hi, I'm [your name]. This is CareBridge, a recovery companion I built for patients here in Frisco, Texas.

**0:06 – 0:24 · "Why CareBridge" (the problem)**
> Going home from the hospital is when recovery really starts, and it's also when patients are most on their own. They leave with pages of instructions and new medications, and their doctor may not hear how they're doing until the next visit. CareBridge closes that gap. It turns discharge papers into a daily plan, reminds patients when something is due, and keeps their care team updated.

**0:24 – 0:27 · "Part 1: Patient experience"**
> Let's start with what a patient sees after leaving the hospital.

**0:27 – 0:38 · Sign-in and today's checklist**
> Frisco is one of the fastest-growing cities in Texas, and a lot of families here are juggling work, school, and the commute on the Tollway. When someone comes home from the hospital, their instructions can get lost. CareBridge turns them into a simple checklist.

**0:38 – 0:42 · Items awaiting review**
> These two items came from a scan, so they're marked "awaiting review" until a doctor confirms them.

**0:42 – 0:47 · Reminders**
> Patients can turn on reminders, so their phone tells them when each medication or exercise is due.

**0:47 – 0:53 · Scan discharge instructions**
> Here, a patient takes a photo of their printed or handwritten discharge papers, and AI reads the medications and exercises off the page.

**0:53 – 1:01 · Recovery journal**
> The journal is a private place to check in each day. Patients pick a mood and write how they're really doing.

**1:01 – 1:08 · Crisis support screen**
> If an entry shows signs of suicidal thoughts, CareBridge immediately shows crisis resources, like calling or texting 988, and alerts the care team.

**1:08 – 1:14 · Recovery companion**
> The companion chatbot answers questions, but only about the patient's own care plan. It never diagnoses or changes medications.

**1:14 – 1:18 · My records**
> And patients can download their full record instantly.

**1:18 – 1:22 · "Part 2: Provider dashboard"**
> Now let's switch to the doctor's side.

**1:22 – 1:30 · Sign-in and patient list**
> A care team at a Frisco-area clinic sees every patient in one list, sorted so the people who need attention most are at the top.

**1:30 – 1:37 · Risk alerts**
> Here's the alert from the journal entry we just wrote. The doctor acknowledges it, and that action is recorded.

**1:37 – 1:42 · Adherence**
> This shows how often each medication and exercise was actually done over the last week.

**1:42 – 1:46 · Mood trend**
> The mood chart shows whether the patient is trending up or down.

**1:46 – 1:53 · Journal screening**
> Each journal entry gets a screening result, with the exact words that triggered it, so the doctor can see the evidence.

**1:53 – 2:00 · Approve the scanned plan**
> Remember the two items from the photo? The doctor reviews them and approves them, so nothing the AI reads goes unchecked.

**2:00 – 2:05 · Training lock**
> Texas has its own privacy law, House Bill 300. Staff can't open patient records unless their privacy training is up to date.

**2:05 – 2:15 · Compliance dashboard**
> An administrator can see who has completed training, plus an audit log of every sign-in, every record viewed, and every blocked attempt. *(Optional:)* That's what Texas regulators expect from anyone handling health data.

**2:15 – 2:19 · "Part 3: How the code works"**
> So how does it all work behind the scenes?

**2:19 – 2:34 · Architecture**
> CareBridge is written in Python using Flask. The patient app and the doctor portal both talk to one server. That server stores health data encrypted, uses Claude for the AI features, sends alerts by text or email, and exports records in FHIR, the standard format hospitals use.

**2:34 – 2:50 · Following one journal entry**
> Let's follow one journal entry. The patient writes it, and it's encrypted and saved. Claude answers the standard suicide-screening questions. Then my own Python rules, not the AI, decide the risk level. If it's high, the patient sees crisis help, and the doctor gets an alert.

**2:50 – 2:53 · "Part 4: The challenge"**
> Here's the hardest problem I had to solve.

**2:53 – 3:06 · Problem and solution**
> What happens if the AI goes down? If the screener quietly reports "no risk," someone in crisis could be missed. That was the scariest failure I could imagine, so I designed the app to never fail open.

**3:06 – 3:20 · The code**
> Here's the code. If the AI call fails for any reason, the app falls back to checking for crisis keywords. Any match is treated as high risk and flagged for a clinician to review. A failure makes the app more careful, not less.

**3:20 – 3:26 · Thank-you card**
> CareBridge helps Frisco patients recover safely at home, and keeps their care team in the loop. Thank you!
