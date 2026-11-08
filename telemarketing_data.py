# Telemarketing scripts shown on the /admin page. Edit freely or add more: {"title", "when", "text"}.
# [Your Name] is filled in automatically from the name box on the page.
# {PRICES} is filled in from the PLANS list in main.py, so prices here never go stale.
# Other [brackets] are for the caller to fill in live.
SCRIPTS = [
 {"title": "Opening: cold call", "when": "The first 30 seconds. The goal is permission to explain, not to sell.", "text": """"Hello, my name is [Your Name] with titlepen.com. Am I speaking with [Prospect Name]?"

(If yes) "Thanks, [Prospect Name]. I know you're busy, so I'll be quick. Do you have 30 seconds for me to tell you why I'm calling?"

"TitlePen is a website that helps title and escrow companies with their marketing. You tell it what you need in plain English, like a monthly update for your agents or a wire-fraud warning for buyers, and about a minute later you have a draft you can edit and send."

"I'm calling because most title offices want to stay in front of their agents and lenders, but nobody has time to write the newsletters, emails, and flyers. That's the gap we fill."

"Would it be alright if I showed you how it works? You can try it free: two drafts, no credit card.\""""},

 {"title": "Gatekeeper or front desk", "when": "When someone other than the decision maker picks up.", "text": """"Hi, this is [Your Name] with titlepen.com. Could you tell me who handles marketing or agent outreach for the office?"

(If they ask what it's about) "We help title companies write their newsletters and agent emails faster. I'd just like to share a quick free option with whoever handles that."

(If the person is unavailable) "No problem. When is a good time to reach [Prospect Name]? And what's the best email to send a short note to?"

Tip: be friendly and honest about who you are and why you're calling. Never pretend to be a client or a friend of the owner."""},

 {"title": "The pitch: what it does and how it helps their business", "when": "After they say yes to hearing more. Keep it conversational, and pause for questions.", "text": """WHAT IT DOES
"TitlePen writes first drafts of your marketing: newsletters, emails to agents and lenders, social posts, flyers, and postcards. You describe what you want, it drafts it, and you edit it right on the page. Then you copy it or download it."

HOW IT HELPS YOUR BUSINESS
1. It saves time. "What usually takes an afternoon takes a few minutes, so marketing stops piling up on top of your files."
2. It keeps you in front of agents and lenders. "Monthly market updates, welcome notes to new agents, lender check-ins, holiday greetings. There are editable templates for all of them."
3. It helps you protect your clients. "There's a ready-made wire-fraud warning and a closing-day checklist you can send to buyers and sellers."
4. It's built with title in mind. "It knows the basics of title search, owner's and lender's policies, and closings, so you're not starting from a blank page."
5. You stay in control. "Nothing is sent for you. You review and edit every draft, then send it from your own email."
6. The price is simple. "{PRICES}. You can cancel anytime."

STRAIGHT TALK (say this, it builds trust)
"The drafts are written by AI, so always read them over before they go out. Title marketing has rules in many states, so have someone with compliance in mind review anything before it's sent."

Do NOT promise: more closings, more referrals, specific results, or that any draft is legally compliant. The "Unlimited" plan is 150 generations a month, so never say it is unlimited."""},

 {"title": "Voicemail (under 30 seconds)", "when": "When it goes to voicemail. Speak slowly, and say the website twice.", "text": """"Hi [Prospect Name], this is [Your Name] with titlepen.com, that's T-I-T-L-E-P-E-N dot com. We help title companies write their newsletters, agent emails, and flyers in minutes, and you can try it free. Again, [Your Name], titlepen.com. Have a great day.\""""},

 {"title": "Handling objections", "when": "Stay calm and friendly. One honest answer, then ask a gentle question. If they say no a second time, thank them and end the call.", "text": """"Not interested."
"Totally understand. Can I ask, is that because marketing is already covered, or just not a priority right now?" (Then listen.)

"We already have someone who does our marketing."
"That's great. Most of our users do too. TitlePen just gives them a faster first draft, so they can spend their time on the personal side. Would it be okay if I sent them a link to try for free?"

"I don't have time for this."
"I get it, that's exactly why I'm calling. A draft takes about a minute. Can I send you a link and let you try it when things calm down?"

"Is this AI? Can I trust it?"
"Yes, it uses AI to write first drafts. You edit everything and decide what goes out, so nothing is sent without you. And I'd always have someone review marketing before it's sent."

"Is it compliant?"
"I can't give legal advice. It's set up to steer away from things like promising rates or referral incentives, but every draft should still be reviewed for your state's rules."

"How much does it cost?"
"It's free to try with two drafts. After that: {PRICES}. You can cancel anytime."

"Just send me some information."
"Happy to. What's the best email for you? I'll send a short note with the link, and I'll follow up [day] to see if you had a chance to try it.\""""},

 {"title": "Closing: the next step", "when": "When they're open to trying it. Make the next step small and easy.", "text": """"The easiest way to see if it's useful is to try it. It takes about two minutes."

"Go to titlepen.com and click Start free. You only need an email and a password. You'll get two free drafts."

"Try asking it for something you actually need, like 'a monthly update for the real estate agents we work with.' Then look at the draft and see if it sounds like you."

"Can I check back with you on [day] to hear what you thought?"

(If they sign up while on the call) "Wonderful. If you like it and want more than two drafts, the plans are on the home page, and you can change or cancel anytime.\""""},

 {"title": "Follow-up email", "when": "Send right after the call, or after a voicemail.", "text": """Subject: Your free TitlePen drafts, [Prospect Name]

Hi [Prospect Name],

Thanks for taking my call. I'm [Your Name] with TitlePen (titlepen.com).

TitlePen helps title companies write newsletters, agent emails, wire-fraud warnings, and flyers in minutes. You describe what you need, and it drafts it for you to edit and send.

You can try it free, with two drafts and no credit card, at titlepen.com.

I'll check back on [day]. If you have any questions before then, just reply to this email.

[Your Name]
titlepen.com"""},
]
