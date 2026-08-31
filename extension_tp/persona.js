// Baked synthetic persona for the Teleperformance (iCIMS) application.
// Residence is a TP ALLOWED state (Ohio) — TP restricts hiring to a 38/39-state list, and this
// posting family is often "based in Ohio". English-native, no Spanish, CSR-experienced. The email
// is an @takhet.com mailbox on our server (the account verification code lands there — see the
// popup's "Get code" button, which reads it from the server). Change the persona by editing here.
const TP_PERSONA = {
  first_name: "Olivia",
  middle_name: "",
  last_name: "Bennett",
  full_name: "Olivia Bennett",
  email: "olivia.bennett2311@takhet.com",
  password: "Jf7xQ2wnpkV9!",          // upper+lower+digit+symbol (iCIMS complexity)
  phone_digits: "+16145550142",        // country code, no spaces/hyphens (the field demands this)
  phone_type: "Mobile",
  address: "1200 Market Street",
  city: "Columbus",
  state_code: "OH",
  state_full: "Ohio",
  zip: "43215",
  country: "United States",
  // screener facts
  english: "native",                   // US persona -> native English
  bilingual: false,                    // set true only for a Bilingual posting
  education_level: "Bachelor",
  how_heard: ["Job Board", "Indeed", "Google Search", "Media", "Other/None"],  // try in order
};
