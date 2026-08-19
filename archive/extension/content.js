// Quick Apply — content script
// Runs on every page, listens for fill commands from popup

const PROFILE_PATTERNS = {
  'first.?name': 'first_name',
  'last.?name|surname': 'last_name',
  'full.?name|your.?name|^name\\b': 'full_name',
  '\\bemail\\b': 'email',
  '\\bphone\\b|mobile|tel\\b': 'phone',
  'linkedin': 'linkedin',
  'country': 'country',
  'location|city': 'location',
};

const ANSWER_PATTERNS = {
  'authorized.*work|work.*authorization|legally.*work|eligible.*work|right to work': 'Yes',
  'require.*sponsor|visa.*sponsor|need.*sponsor': 'No',
  'how soon.*start|start date|earliest.*start|when.*start|available.*start': 'Immediately',
  'available.*work.*hours|available.*schedule|work.*schedule': 'Yes',
  'available.*remote|work.*remote|comfortable.*remote': 'Yes',
  'years.*experience|experience.*years|how many years': '5',
  'previous.*experience|relevant.*experience|do you have.*experience': 'Yes',
  'highest.*education|education.*level|degree': "Bachelor's Degree",
  'where.*located|current.*location': 'Chicago, IL',
  'time.?zone': 'Eastern',
  'willing.*relocate': 'No',
  'salary.*expect|desired.*salary|compensation.*expect|salary.*requirement|desired.*pay': 'Negotiable',
  '18.*years.*old|over.*18|at least 18': 'Yes',
  'background.*check|consent.*background|criminal.*check': 'Yes',
  'acknowledge|agree|confirm|consent': 'Yes',
  'have.*computer|reliable.*internet': 'Yes',
  'hear about|how.*find|source|referr': 'Job Board',
  'cover.?letter|why.*interested|why.*apply|why.*join|why.*want':
    'I am excited about this opportunity because it aligns perfectly with my professional experience and career goals. I bring 5 years of relevant experience and am passionate about contributing to a team that values excellence and innovation.',
  'define.*customer.*experience|excellent.*customer':
    'An excellent customer experience means anticipating needs, resolving issues efficiently, communicating clearly, and leaving every interaction better than it started.',
  'accommodat|disability.*interview': 'No',
  'additional.*information|anything.*else': 'Thank you for considering my application.',
};


function getLabel(el) {
  // Try <label> wrapping the element
  let label = el.closest('label');
  if (label) return label.innerText.trim();

  // Try <label for="id">
  if (el.id) {
    let lb = document.querySelector(`label[for="${el.id}"]`);
    if (lb) return lb.innerText.trim();
  }

  // Previous sibling
  let prev = el.previousElementSibling;
  if (prev && ['LABEL', 'SPAN', 'DIV', 'P'].includes(prev.tagName)) {
    return prev.innerText.trim();
  }

  // Walk up parents
  let parent = el.parentElement;
  for (let i = 0; i < 3 && parent; i++) {
    let text = '';
    for (let child of parent.childNodes) {
      if (child.nodeType === 3) text += child.textContent;
      if (['LABEL', 'SPAN', 'P', 'DIV'].includes(child.tagName) &&
          !child.querySelector('input,select,textarea')) {
        text += child.innerText;
      }
    }
    text = text.trim();
    if (text && text.length > 2 && text.length < 200) return text;
    parent = parent.parentElement;
  }

  return el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
}


function extractFields() {
  const results = [];
  const elements = document.querySelectorAll('input, select, textarea');

  for (const el of elements) {
    if (!el.offsetParent && el.type !== 'file' && el.type !== 'hidden') continue;
    if (el.type === 'hidden') continue;

    const field = {
      el,
      tag: el.tagName.toLowerCase(),
      type: el.type || 'text',
      name: el.name || '',
      id: el.id || '',
      value: el.value || '',
      placeholder: el.placeholder || '',
      required: el.required || el.getAttribute('aria-required') === 'true',
      label: getLabel(el),
    };

    // Build selector
    if (el.id) field.selector = '#' + CSS.escape(el.id);
    else if (el.name) field.selector = el.tagName.toLowerCase() + '[name="' + el.name + '"]';
    else field.selector = '';

    // Get select options
    if (el.tagName === 'SELECT') {
      field.options = Array.from(el.options)
        .map(o => ({ text: o.text.trim(), value: o.value }))
        .filter(o => o.value);
    }

    results.push(field);
  }
  return results;
}


function matchAnswer(label, field, profile, savedAnswers) {
  const ll = label.toLowerCase().trim();
  if (!ll) return null;

  // File upload → skip (user handles resume manually or via drag)
  if (field.type === 'file') return null;

  // Check saved answers from QuestionBank (exact match on normalized label)
  for (const [question, answer] of Object.entries(savedAnswers)) {
    if (question.toLowerCase().trim() === ll) return answer;
  }

  // Profile patterns
  for (const [pattern, key] of Object.entries(PROFILE_PATTERNS)) {
    if (new RegExp(pattern, 'i').test(ll)) {
      return profile[key] || null;
    }
  }

  // Answer patterns
  for (const [pattern, answer] of Object.entries(ANSWER_PATTERNS)) {
    if (new RegExp(pattern, 'i').test(ll)) return answer;
  }

  // Select with preferred options
  if (field.options) {
    const preferred = ['yes', 'united states', 'immediately', 'decline', 'prefer not'];
    for (const pref of preferred) {
      for (const opt of field.options) {
        if (opt.text.toLowerCase().includes(pref)) return opt.text;
      }
    }
  }

  // Checkbox: agree/acknowledge
  if (field.type === 'checkbox') {
    if (['agree', 'acknowledge', 'consent', 'accept'].some(x => ll.includes(x))) {
      return 'true';
    }
  }

  return null;
}


function setNativeValue(el, value) {
  // Trigger React-compatible value change
  const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype, 'value'
  )?.set;
  const nativeTextAreaValueSetter = Object.getOwnPropertyDescriptor(
    window.HTMLTextAreaElement.prototype, 'value'
  )?.set;

  if (el.tagName === 'TEXTAREA' && nativeTextAreaValueSetter) {
    nativeTextAreaValueSetter.call(el, value);
  } else if (nativeInputValueSetter) {
    nativeInputValueSetter.call(el, value);
  } else {
    el.value = value;
  }

  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  el.dispatchEvent(new Event('blur', { bubbles: true }));
}


function fillField(field, answer) {
  const el = field.el;
  try {
    if (field.type === 'checkbox') {
      if (!el.checked && answer.toLowerCase() === 'true') {
        el.click();
      }
      return true;
    }

    if (field.type === 'select-one' || field.tag === 'select') {
      // Try exact match first
      for (const opt of el.options) {
        if (opt.text.trim() === answer || opt.value === answer) {
          el.value = opt.value;
          el.dispatchEvent(new Event('change', { bubbles: true }));
          return true;
        }
      }
      // Partial match
      for (const opt of el.options) {
        if (opt.text.toLowerCase().includes(answer.toLowerCase())) {
          el.value = opt.value;
          el.dispatchEvent(new Event('change', { bubbles: true }));
          return true;
        }
      }
      return false;
    }

    // Text/textarea
    el.focus();
    setNativeValue(el, answer);
    return true;

  } catch (e) {
    console.warn('Quick Apply: fill error', e);
    return false;
  }
}


function fillOne(selector, value, type) {
  const el = document.querySelector(selector);
  if (!el) return false;

  if (type === 'checkbox') {
    if (!el.checked) el.click();
    return true;
  }
  if (type === 'select-one' || el.tagName === 'SELECT') {
    for (const opt of el.options) {
      if (opt.text.trim() === value || opt.text.toLowerCase().includes(value.toLowerCase())) {
        el.value = opt.value;
        el.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
      }
    }
    return false;
  }

  el.focus();
  setNativeValue(el, value);
  return true;
}


// Listen for messages from popup
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === 'fill') {
    const profile = msg.profile || {};
    const savedAnswers = msg.answers || {};
    const fields = extractFields();
    let filled = 0;
    let skipped = 0;
    const unanswered = [];

    for (const field of fields) {
      if (['submit', 'button', 'hidden', 'search'].includes(field.type)) continue;
      if (field.value && field.type !== 'select-one') continue;
      if (field.type === 'file') continue;

      const label = field.label;
      const answer = matchAnswer(label, field, profile, savedAnswers);

      if (answer === null) {
        if (field.required && label && label.length > 3) {
          unanswered.push({
            label: label.substring(0, 100),
            type: field.type,
            selector: field.selector,
            options: (field.options || []).map(o => o.text),
          });
        }
        skipped++;
        continue;
      }

      if (fillField(field, answer)) {
        filled++;
      } else {
        skipped++;
      }
    }

    sendResponse({ filled, skipped, unanswered });
    return true;
  }

  if (msg.action === 'fill_one') {
    const ok = fillOne(msg.selector, msg.value, msg.type);
    sendResponse({ ok });
    return true;
  }

  if (msg.action === 'ping') {
    sendResponse({ ok: true });
    return true;
  }
});
