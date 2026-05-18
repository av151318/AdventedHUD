# Soul Template

> **Instructions for the user**  
> Copy this file and save it as `soul.md` in your Obsidian vault.  
> Fill it out honestly and in your own words. This becomes your personal source of truth for roles, goals, and how you want to live.  
> You do not need to finish it in one sitting.

---

## 1. Mission Statement

What is your personal mission? If you had to capture the impact you want to make in one or two sentences, what would it be?

---

## 2. Values & Principles

What values and principles are most important to you? What do you want to live by?

---

## 3. Balance (Life Areas)

How do you want to show up in these areas of your life?

- **Physical**  
- **Social / Emotional**  
- **Mental**  
- **Spiritual**  

---

## 4. Roles

List the main roles you currently play in your life (these can be personal, professional, or aspirational).

For each role, write a short description of what that role means to you.

Example format:
- Role: Engineer  
  Description: I design and build reliable hardware systems.

- Role: Friend  
  Description: I show up for the people close to me with presence and care.

---

## 5. Goals per Role

For each role above, what are your long-term or meaningful goals?

For each goal, include:
- What “done” or meaningful progress looks like (qualitative)
- Any near-term outcomes you’re aiming for (optional)

Example format:
- Role: Engineer  
  Goal: Build a reliable PCB prototyping workflow  
  Meaningful progress: I can consistently design, order, and bring up functional boards without major blockers.

- Role: Friend  
  Goal: Maintain deep, consistent relationships  
  Meaningful progress: I have regular meaningful conversations with my close friends.

---

## 6. Current Focus & Near-Term Priorities

What are you currently working on or focused on in the next few weeks or months?

What results or progress would feel meaningful to you right now?

---

## 7. Talents, Strengths & Energy

What are you naturally good at?  
What kinds of work or activities energize you?

What drains you or makes you feel ineffective?

---

## 8. Additional Notes

Anything else you want to capture about how you want to live or operate?

---

**Notes for the agent (not for the user):**

This template is designed so the agent can guide the user conversationally through the sections while using meta-prompting. The agent should help the user produce rich answers, then later extract the atomic structure:

- Mission (from section 1 + supporting context)
- Roles (from section 4)
- Goals (from section 5, tied to roles)
- Supporting context (values, balance, talents, etc.) used for better classification and the decision matrix.

The goal is to produce a `soul.md` that contains both rich personal context and clear atomic data the MCP can rely on.
