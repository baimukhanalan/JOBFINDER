// Apply Assist — baked profile + answer bank for Jordan Sample.
// Self-contained: no server call, so nothing can hang. Edit values here to update.
window.APPLY_PROFILE = {
  first_name: "Jordan",
  last_name: "Sample",
  full_name: "Jordan Sample",
  email: "jordan.sample.demo@example.com",
  phone: "(555) 555-0100",
  phone_digits: "5555550100",
  address: "Springfield, IL",
  city: "Springfield",
  state: "IL",
  state_full: "Illinois",
  zip: "62701",
  country: "United States",
  country_code: "US",
  linkedin: "",
  website: "",
  years_experience: "15",
  education_level: "Bachelor's Degree",
  school: "",
  desired_salary: "Negotiable",
  available_start: "Immediately",
  notice_period: "Immediately",
  timezone: "Central Time (US)",
  // Deterministic eligibility — only auto-answered for US / unspecified country.
  work_authorized_us: "Yes",
  needs_sponsorship: "No",
  over_18: "Yes",
  background_check_consent: "Yes",
  criminal_record: "No",
  willing_relocate: "No", // matches the batch engine (relocation rule -> No)
  remote_ok: "Yes",
  has_equipment: "Yes",
  gender: "Decline to self-identify",
  race: "Decline to self-identify",
  veteran: "I am not a protected veteran",
  disability: "I do not have a disability",
  hispanic: "No",
  source: "Online job search",
};

// Open-ended answers (first person, grounded in the real résumé). Keys are matched
// by regex against the question text; first match wins. Used for textareas / long Qs.
window.APPLY_ANSWERS = [
  [/cover ?letter|why (are|do) you (interested|want|applying)|why (this|our) (role|company|position|team)|why (would you|should we)|tell us why|what (interests|excites) you|motivat/i,
    "With 15 years on the frontline of customer support — phone, live chat, and email — I'm drawn to this role because it's exactly the work I do best and genuinely enjoy. I've consistently held 95%+ CSAT in high-volume e-commerce and SaaS environments, I'm trusted with the toughest escalations and billing disputes, and I've worked fully remote since 2019 with a reliable setup. I'd bring that same calm, customer-first approach to your team from day one."],

  [/(describe|tell us about|give an example|share|a time).*(difficult|upset|angry|frustrat|irate|challenging) (customer|client|situation|interaction)|de-?escalat/i,
    "A customer once reached out furious about being double-billed and threatening to cancel. I let them vent without interrupting, acknowledged the frustration, and confirmed I'd own it personally. I found the duplicate charge, processed the refund while they were still on the line, and explained how to avoid it going forward. They not only stayed but later thanked me by name. My approach is always: listen first, take ownership, fix it fast, follow up."],

  [/(what|how).*(great|excellent|good|quality) (customer (service|experience|support))|define (customer|great|excellent)|mean.*customer (service|experience)/i,
    "Great customer service means anticipating needs, communicating clearly, resolving the issue efficiently, and leaving every interaction better than it started. It's owning the problem so the customer never has to chase you, and treating each person with patience and respect — even when they're upset."],

  [/(your )?(greatest )?(strength|skills|qualif|why are you|what makes you|good fit|right (candidate|fit)|bring to)/i,
    "My strengths are empathy under pressure, fast and accurate communication (65+ WPM), and deep experience across CRMs and help-desk tools. I ramp quickly on new products, I'm a top-quartile CSAT performer, and I'm an individual contributor by choice — I genuinely like frontline support work and the problem-solving it takes."],

  [/(describe|tell us about).*(remote|work from home|wfh)|experience.*remot|comfortable.*remot|remote.*experience/i,
    "I've worked fully remote since 2019 with a dedicated home office, reliable high-speed internet, and a quiet, professional environment. I'm self-disciplined, responsive on chat and email, and used to managing my own queue and schedule without supervision."],

  [/(weakness|area.*improve|improve.*on)/i,
    "Earlier in my career I tended to take on too much myself rather than escalating. Over time I've learned to recognize when looping in a specialist gets the customer a faster resolution, and I now balance ownership with knowing when to hand off."],

  [/(salary|compensation|pay).*(expect|require|desired|range)|expected (salary|pay|comp)/i,
    "Negotiable / open to discussion based on the full role and benefits."],

  [/where.*(hear|find|learn).*(about|of)|how did you (hear|find|learn)|source|referr/i,
    "Through an online job search."],

  [/(anything else|additional (information|comments)|is there anything|other.*(info|comments))/i,
    "Thank you for considering my application — I'd welcome the chance to discuss how my support background fits the role."],

  [/(tools|crm|software|systems|help ?desk).*(used|familiar|experience)|experience.*with (tools|crm|software)/i,
    "I've worked across Zendesk, Salesforce Service Cloud, Freshdesk, Intercom, and Gorgias, plus standard ticketing, live-chat, and knowledge-base tools. I pick up new platforms quickly."],
];
