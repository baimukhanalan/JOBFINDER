"""Synthetic demo candidate generator for the ETALON fill.

When a human clicks "Заполнить"/generate on the /catalog demo, we do NOT use a real
roster candidate — we invent a fresh, entirely FICTIONAL applicant tailored to the job
(a random name, a derived @takhet.com email, a plausible résumé) so the demo shows a
complete fill without ever exposing a real person. Real applications still use the real
roster via catalog_drafts.pick_candidate (this module is demo-only).

The persona's NATIONALITY matches the JOB'S COUNTRY (parsed from the location, falling back
to the region tag, else Kazakhstan) so that work-authorization answers are truthful and
consistent: a US-based role gets an American ("authorized in the US: yes"), a Netherlands
role gets a Dutch person, a Tbilisi role a Georgian, etc. — never a Kazakhstani in Almaty
claiming US work authorization. A location-less role for a LatAm-EXCLUSIVE employer (e.g.
GoFasti's "TopTalent from LatAm" / "based in Latin America") gets a real Latin-American
persona so the honest "based in Latin America? -> Yes" isn't a guaranteed geo rejection.
Built by the local LLM with a deterministic fallback so a demo click never fails.
"""
from __future__ import annotations

import json
import os
import random
import re

from backend.services.tailor.tailor import _llm_complete
from backend.tools.catalog_drafts import LATAM_COUNTRIES, derive_email

# --- resolve the job's country --------------------------------------------------
# location keyword -> canonical country (first match wins; order matters for overlaps)
_LOC_COUNTRY = [
    (re.compile(r"(?i)\b(united states|u\.?s\.?a\.?|u\.?s\.?\b|america|remote[- ]?us)\b"), "United States"),
    (re.compile(r"(?i)\b(canada|canadian)\b"), "Canada"),
    (re.compile(r"(?i)\b(united kingdom|u\.?k\.?\b|england|scotland|wales|london)\b"), "United Kingdom"),
    (re.compile(r"(?i)\b(netherlands|holland|amsterdam)\b"), "Netherlands"),
    (re.compile(r"(?i)\b(germany|deutschland|berlin|munich)\b"), "Germany"),
    (re.compile(r"(?i)\b(ireland|dublin)\b"), "Ireland"),
    (re.compile(r"(?i)\b(france|paris)\b"), "France"),
    (re.compile(r"(?i)\b(spain|madrid|barcelona)\b"), "Spain"),
    (re.compile(r"(?i)\b(portugal|lisbon)\b"), "Portugal"),
    (re.compile(r"(?i)\b(poland|warsaw|krakow)\b"), "Poland"),
    (re.compile(r"(?i)\b(brazil|brasil|sao paulo|são paulo)\b"), "Brazil"),
    (re.compile(r"(?i)\b(mexico|méxico)\b"), "Mexico"),
    (re.compile(r"(?i)\b(argentina|buenos aires)\b"), "Argentina"),
    (re.compile(r"(?i)\b(india|bengaluru|bangalore|mumbai|delhi)\b"), "India"),
    (re.compile(r"(?i)\b(australia|sydney|melbourne)\b"), "Australia"),
    (re.compile(r"(?i)\b(singapore)\b"), "Singapore"),
    (re.compile(r"(?i)\b(ukraine|kyiv|kiev|lviv)\b"), "Ukraine"),
    (re.compile(r"(?i)\b(serbia|belgrade)\b"), "Serbia"),
    (re.compile(r"(?i)\b(turkey|t[uü]rkiye|istanbul)\b"), "Turkey"),
    (re.compile(r"(?i)\b(armenia|yerevan)\b"), "Armenia"),
    (re.compile(r"(?i)\b(azerbaijan|baku)\b"), "Azerbaijan"),
    (re.compile(r"(?i)\b(kyrgyzstan|bishkek)\b"), "Kyrgyzstan"),
    (re.compile(r"(?i)\b(uzbekistan|tashkent)\b"), "Uzbekistan"),
    (re.compile(r"(?i)\b(tajikistan|dushanbe)\b"), "Tajikistan"),
    (re.compile(r"(?i)\b(tbilisi)\b|\bgeorgia\b(?!,?\s*(?:us|usa|united states|atlanta))"), "Georgia"),
    (re.compile(r"(?i)\b(kazakhstan|almaty|astana|nur[- ]?sultan)\b"), "Kazakhstan"),
]
_REGION_COUNTRY = {"US": "United States", "CA": "Canada", "UK": "United Kingdom"}
# The catalog is remote US/CA roles on US ATS boards (Greenhouse/Ashby/Lever/Workable), so a
# job the classifier couldn't place (location just "Remote", regions=[] / OTHER) is
# overwhelmingly a US company — apply as a US persona (US-authorized), NOT a Kazakhstani. An
# explicit non-US location is parsed above and still wins (Salmon's "Kazakhstan; Kyrgyzstan"
# → Kazakhstan; a Netherlands role → Dutch); only the truly-unknown fallback lands here.
# (Was "Kazakhstan" — a leftover agency default that made a US company like thinkacademyus
# get a Kazakh persona claiming Kazakhstan citizenship on a US role. Fixed 2026-08-25.)
_DEFAULT_COUNTRY = "United States"

# Some employers hire EXCLUSIVELY talent RESIDENT in Latin America (GoFasti et al.) and
# auto-reject anyone outside the region regardless of a perfect fill. When the location does
# NOT already pin a concrete country, this signal in the JD text routes the etalon to a real
# LatAm country so "based in Latin America? -> Yes" is truthful. Kept narrow (a residence /
# hiring / talent context next to "Latin America"/"LatAm") so a mere market mention
# ("customers across Latin America") does not trip it.
_LATAM_RESIDENCE_RE = re.compile(
    r"(?i)(?:"
    r"(?:based|located|reside|residing|living|live|work(?:ing)?)\s+(?:remotely\s+)?"
    r"(?:in|from|within|across)\s+(?:latin\s*america|latam)"
    r"|(?:talent|developers?|designers?|engineers?|candidates?|professionals?|hires?|"
    r"team\s+members?)\s+(?:from|based\s+in|located\s+in|in)\s+(?:latin\s*america|latam)"
    r"|from\s+latam\b"
    r"|top\s*talent\s+from\s+latam"
    r"|latin\s*american\s+(?:countr|candidate|talent|resident|national|professional)"
    r"|(?:hir\w+|recruit\w+)\s+(?:talent\s+)?(?:exclusively\s+)?(?:based\s+)?(?:in|from)\s+"
    r"(?:latin\s*america|latam)"
    r")")


def _requires_latam(job: dict) -> bool:
    text = " ".join(str(job.get(k) or "") for k in ("title", "location", "description"))
    return bool(_LATAM_RESIDENCE_RE.search(text))

_CITIZEN = {"United States": "U.S. Citizen", "United Kingdom": "British Citizen",
            "Canada": "Canadian Citizen", "Mexico": "Mexican Citizen",
            "Colombia": "Colombian Citizen", "Argentina": "Argentine Citizen",
            "Chile": "Chilean Citizen", "Peru": "Peruvian Citizen",
            "Brazil": "Brazilian Citizen"}

# Name banks per country, split by gender (male/female/last) so the /catalog "Заполнить" M/Ж
# choice picks a gender-appropriate first name. OUR source of names (not just the LLM fallback):
# `_pick_name` chooses first+last from here, avoiding recently-used names, and pins that name
# into the LLM prompt — the local LLM is stateless and otherwise collapses to the same handful
# of "favourite" names on every fill. Banks are large so repeats are rare even before history.
_NAMES = {
    "United States": {
        "male": ["James", "Michael", "Daniel", "Ethan", "Noah", "Mason", "Logan", "Jackson", "Aiden",
         "Caleb", "Owen", "Henry", "Nathan", "Isaac", "Evan", "Julian", "Adrian", "Miles",
         "Lucas", "Benjamin", "Samuel", "Wyatt", "Dylan", "Gavin", "Hunter", "Cole", "Brandon",
         "Austin", "Elliot", "Nolan", "Spencer", "Tristan", "Bryce", "Preston", "Colton", "Grant",
         "William", "Alexander", "Jacob", "Matthew", "Joseph", "David", "Andrew", "Joshua",
         "Christopher", "Anthony", "Ryan", "Nicholas", "Tyler", "Aaron", "Jack", "Luke", "Gabriel",
         "Connor", "Cameron", "Landon", "Dominic", "Ian", "Adam", "Christian", "Jonathan",
         "Jordan", "Brady", "Chase", "Zachary", "Everett", "Finn", "Graham", "Harrison", "Jasper",
         "Keegan", "Levi", "Maxwell", "Oscar", "Parker", "Quentin", "Reid", "Sawyer", "Theodore",
         "Victor", "Weston", "Xander", "Zane", "Beau", "Declan", "Emmett", "Ford", "Griffin",
         "Holden", "Jude", "Knox", "Marcus", "Rhys", "Silas", "Tobias", "Walker", "Carson"],
        "female": [
         "Emily", "Olivia", "Grace", "Ava", "Sophia", "Chloe", "Lily", "Hannah", "Zoe", "Nora",
         "Ruby", "Claire", "Violet", "Stella", "Aubrey", "Naomi", "Paige", "Vivian", "Ella",
         "Hazel", "Aurora", "Savannah", "Brooke", "Delaney", "Autumn", "Sadie", "Willow", "Piper",
         "Reese", "Sienna", "Elena", "Faith", "Harper", "Maya", "Layla", "Alice", "Emma",
         "Abigail", "Madison", "Elizabeth", "Charlotte", "Amelia", "Evelyn", "Avery", "Scarlett",
         "Victoria", "Aria", "Penelope", "Nova", "Camila", "Lucy", "Eleanor", "Natalie", "Addison",
         "Bella", "Skylar", "Leah", "Audrey", "Samantha", "Anna", "Allison", "Gabriella", "Quinn",
         "Josephine", "Clara", "Ivy", "Adeline", "Cora", "Genevieve", "Iris", "Juliette", "Lydia",
         "Margaret", "Nina", "Ophelia", "Rosalie", "Tessa", "Valentina", "Wren", "Daisy", "Eloise",
         "Fiona", "Georgia", "June", "Kira", "Lila", "Norah", "Rose", "Selena", "Talia", "Vera"],
        "last": ["Carter", "Bennett", "Foster", "Hayes", "Brooks", "Parker", "Ellis", "Reed", "Coleman",
         "Sullivan", "Fleming", "Dawson", "Harrington", "Blake", "Morrison", "Sherwood",
         "Whitaker", "Preston", "Underwood", "Hollis", "Barrett", "Cross", "Mercer", "Vaughn",
         "Ashby", "Lang", "Sutton", "Pierce", "Rowe", "Kirby", "Nash", "Chase", "Donovan",
         "Hale", "Bishop", "Sawyer", "Frost", "Wells", "Boyd", "Marsh", "Quinn", "Bradley",
         "Hensley", "Delgado", "Maddox", "Shepherd", "Alcott", "Ramsey", "Beckett", "Callahan",
         "Fitzgerald", "Lockwood", "Prescott", "Redding", "Sinclair", "Thatcher", "Winslow",
         "York", "Abbott", "Carver", "Dunn", "Everett", "Fairbanks", "Holloway", "Anderson",
         "Bailey", "Bryant", "Campbell", "Cooper", "Duncan", "Edwards", "Ferguson", "Gardner",
         "Griffin", "Hamilton", "Harper", "Hudson", "Jennings", "Kennedy", "Lambert", "Lawson",
         "Lloyd", "Matthews", "Newton", "Norris", "Oliver", "Osborne", "Palmer", "Patterson",
         "Payne", "Porter", "Powell", "Reeves", "Rhodes", "Riley", "Roberts", "Robinson",
         "Russell", "Sanders", "Sharpe", "Simmons", "Stafford", "Stanton", "Stephens", "Stevenson",
         "Stokes", "Sutcliffe", "Tate", "Terry", "Thornton", "Tucker", "Turner", "Wade", "Wallace",
         "Walsh", "Ward", "Warren", "Watson", "Webb", "Weston", "Wheeler", "Whitfield", "Wilder",
         "Willis", "Wolfe", "Woods", "Yates", "Zimmerman", "Ackerman", "Ainsworth", "Bancroft",
         "Barlow", "Braddock", "Cardwell", "Chandler", "Cromwell", "Dalton", "Eastman", "Fenwick",
         "Garrison", "Hadley", "Harlow", "Ingram", "Kingston", "Merrick", "Norwood", "Ashford",
         "Bexley", "Caldwell", "Denton", "Ashcroft"]},
    "Canada": {
        "male": ["Liam", "Owen", "Nathan", "Aiden", "William", "Samuel", "Felix", "Thomas", "Gabriel",
         "Simon", "Adam", "Elliot", "Xavier", "Nicolas", "Antoine", "Julien", "Mathis", "Leo",
         "Cedric", "Marc", "Olivier", "Etienne", "Hugo", "Raphael", "Lucas", "Zachary", "Benjamin",
         "Alexis", "Charles", "Louis", "Philippe", "Vincent", "Maxime", "Guillaume", "Francois",
         "Jerome", "Bastien", "Emile", "Gaspard", "Loic", "Yannick", "Damien", "Fabien", "Renaud",
         "Sylvain", "Tristan", "Aurele", "Corentin", "Malik", "Jacob", "Ryan", "Nolan", "Dominic",
         "Isaac", "Landon", "Hudson", "Rowan", "Beckett", "Theo", "Jasper", "Cole", "Miles"],
        "female": [
         "Charlotte", "Zoe", "Juliette", "Emma", "Chloe", "Camille", "Rose", "Alice", "Beatrice",
         "Margot", "Laurence", "Sadie", "Amelie", "Ophelie", "Clara", "Manon", "Elodie", "Noemie",
         "Sophie", "Genevieve", "Isabelle", "Florence", "Colette", "Anais", "Emilie", "Sarah",
         "Justine", "Marie", "Gabrielle", "Rosalie", "Maeva", "Coralie", "Delphine", "Sabine",
         "Adele", "Aurelie", "Celine", "Elise", "Ines", "Lea", "Maude", "Noelle", "Oceane",
         "Pauline", "Solene", "Valerie", "Jade", "Mia", "Nora", "Olivia", "Penelope", "Sienna"],
        "last": ["Tremblay", "Gagnon", "Roy", "Clarke", "MacKenzie", "Fortin", "Lavoie", "Bergeron",
         "Cote", "Girard", "Morin", "Belanger", "Cardinal", "Beaulieu", "Fraser", "Leclerc",
         "Boucher", "Gauthier", "Pelletier", "Caron", "Simard", "Fontaine", "Bouchard", "Nadeau",
         "Ouellet", "Poirier", "Levesque", "Cloutier", "Dubois", "Lefebvre", "Michaud",
         "Desjardins", "Lapointe", "Mercier", "Bellemare", "Charbonneau", "Robitaille", "Thibault",
         "Vaillancourt", "Cormier", "Aubin", "Delacroix", "Marchand", "Ferland", "Bernard",
         "Blais", "Chartrand", "Denis", "Fournier", "Gagne", "Grenier", "Hebert", "Jodoin",
         "Labrecque", "Langlois", "Lemieux", "Martel", "Nault", "Ostiguy", "Paquette", "Rivard",
         "Sauve", "Turcotte", "Vezina", "Allard", "Boisvert", "Cadieux", "Dupuis", "Gosselin",
         "Houle", "Lacroix", "Perron", "Rochon", "Sirois", "Tessier", "Villeneuve", "Arsenault",
         "Beaudoin", "Comeau", "Doucet", "Gallant", "Landry", "LeBlanc", "Melanson", "Robichaud",
         "Savoie", "Theriault", "Cameron", "Douglas", "Grant", "MacDonald", "Murray", "Nelson",
         "Ross", "Stewart", "Watson", "Bell", "Campbell", "Ferguson", "Hamilton", "Kennedy",
         "Mitchell", "Reid", "Scott", "Young"]},
    "United Kingdom": {
        "male": ["Oliver", "Harry", "George", "Jack", "Charlie", "Thomas", "Arthur", "Alfie", "Edward",
         "Reuben", "Toby", "Louis", "Henry", "Oscar", "Freddie", "Archie", "Theo", "Leo",
         "Finlay", "Rory", "Sebastian", "Hugo", "Callum", "Dexter", "William", "James", "Noah",
         "Jacob", "Charles", "Alexander", "Benjamin", "Samuel", "Joseph", "Daniel", "Isaac", "Max",
         "Logan", "Ethan", "Mason", "Harrison", "Frederick", "Albert", "Stanley", "Wilfred",
         "Ronnie", "Rex", "Otis", "Chester", "Rupert", "Barnaby", "Percy", "Sidney", "Hamish",
         "Angus", "Fraser", "Lachlan", "Gareth", "Nigel", "Desmond", "Cedric", "Godfrey", "Neville"],
        "female": [
         "Amelia", "Isla", "Freya", "Poppy", "Evie", "Florence", "Ivy", "Maisie", "Rosie", "Elsie",
         "Martha", "Bonnie", "Matilda", "Beatrice", "Willow", "Imogen", "Daisy", "Eleanor",
         "Harriet", "Phoebe", "Bethany", "Clara", "Nancy", "Edith", "Olivia", "Emily", "Sophie",
         "Grace", "Lily", "Charlotte", "Alice", "Ella", "Mia", "Isabella", "Rose", "Evelyn",
         "Gracie", "Millie", "Ada", "Penelope", "Ottilie", "Cordelia", "Winifred", "Prudence",
         "Agatha", "Beatrix", "Cecily", "Dorothy", "Estelle", "Flora", "Hermione", "Josephine",
         "Lavinia", "Nell", "Odette", "Primrose", "Rosalind", "Sybil", "Tabitha", "Verity"],
        "last": ["Walker", "Wright", "Hughes", "Hall", "Green", "Baker", "Clarke", "Turner", "Hutchinson",
         "Pearce", "Whitfield", "Redmond", "Ashworth", "Bramwell", "Fairfax", "Holloway",
         "Winterbourne", "Marsh", "Pembroke", "Radcliffe", "Thornton", "Ainsley", "Barlow",
         "Chadwick", "Fletcher", "Hargreaves", "Kingsley", "Lowe", "Merton", "Ormsby", "Prentice",
         "Rutherford", "Sedgwick", "Thorne", "Wakefield", "Yates", "Attwood", "Carlisle", "Danby",
         "Ellison", "Foxton", "Granger", "Hartley", "Ledbury", "Smith", "Jones", "Taylor", "Brown",
         "Williams", "Davies", "Evans", "Roberts", "Johnson", "Robinson", "Wilson", "Wood",
         "Thompson", "Hill", "Harris", "Cooper", "Ward", "Morris", "Moore", "Clark", "Lewis",
         "Jackson", "Watson", "Cook", "Bennett", "Carter", "Bailey", "Parker", "Collins", "Bell",
         "Murphy", "Kelly", "Cox", "Richardson", "Marshall", "Simpson", "Foster", "Gibson",
         "Grant", "Gray", "Hamilton", "Harvey", "Holmes", "Hunt", "Knight", "Lloyd", "Mills",
         "Mitchell", "Newman", "Palmer", "Payne", "Perry", "Poole", "Reeves", "Rhodes", "Riley",
         "Shaw", "Stevens", "Stokes", "Sutton", "Wade", "Webb", "Wells", "Barnes", "Blackwood",
         "Croft", "Godwin", "Hawthorne", "Middleton", "Pennington", "Ravenscroft", "Selby",
         "Underhill", "Wycliffe"]},
    "Kazakhstan": {
        "male": ["Arman", "Dias", "Aibek", "Timur", "Nurlan", "Yerlan", "Bekzat", "Ruslan", "Sanzhar",
         "Adil", "Daniyar", "Kairat", "Yernar", "Azamat", "Nurbek", "Alikhan", "Yerkebulan",
         "Talgat", "Miras", "Serik", "Askar", "Rustem", "Dauren", "Zhandos", "Baurzhan", "Yerbol",
         "Nurzhan", "Aidos", "Almas", "Bakyt", "Damir", "Erasyl", "Galym", "Ilyas", "Kanat", "Madi",
         "Nariman", "Olzhas", "Rakhat", "Temirlan", "Ulan", "Yeldos", "Zhalgas", "Abzal", "Dastan",
         "Erzhan", "Farkhat", "Kuanysh", "Meirzhan", "Nurdaulet", "Sultan", "Zhomart", "Adilet",
         "Didar", "Iskander", "Karim", "Maksat", "Oralbek", "Yerkin", "Zhasulan"],
        "female": [
         "Dana", "Aigerim", "Alina", "Aizhan", "Madina", "Zhanar", "Gulnar", "Assel", "Aisulu",
         "Malika", "Saltanat", "Ainur", "Zarina", "Balzhan", "Gaukhar", "Dinara", "Kamila",
         "Aruzhan", "Nazerke", "Zhuldyz", "Tomiris", "Alua", "Meruert", "Sabina", "Akbota",
         "Bibigul", "Dilnaz", "Elmira", "Fariza", "Aruna", "Botagoz", "Damira", "Farida",
         "Gulzhan", "Indira", "Karlygash", "Moldir", "Nazgul", "Perizat", "Raushan", "Symbat",
         "Togzhan", "Ulzhan", "Venera", "Zhibek", "Aiym", "Bayan", "Elnara", "Gulmira", "Inkar",
         "Kymbat", "Nurgul", "Saule", "Tolganay", "Zhaniya"],
        # Male/base forms only — a female KZ persona's surname is feminized at pick time
        # (_feminize_kz): -ov/-ev/-in -> +a, -uly -> -kyzy. Do NOT put -ova/-kyzy forms here.
        "last": ["Serikuly", "Zhaksybek", "Toleubek", "Amirkhan", "Beisenov", "Yesenov", "Nurpeisov",
         "Sagatov", "Iskakov", "Bekov", "Omarov", "Kassymov", "Zhumabek", "Tulegenov", "Abenov",
         "Dosanov", "Kaliyev", "Seitkali", "Baibek", "Nurlanuly", "Akhmetov", "Suleimenov",
         "Zhaparov", "Musin", "Aitkali", "Bolatov", "Mukhamedzhanov", "Sadykov", "Tazhibaev",
         "Utegenov", "Karimov", "Rakhimov", "Bekbolat", "Zhaksylyk", "Amanzhol", "Serikbay",
         "Turlybek", "Ospanov", "Duisenbek", "Khamitov", "Abdrakhmanov", "Baimukhanov", "Yerzhanov",
         "Kudaibergenov", "Orazbek", "Sydykov", "Temirbekov", "Zhunusov", "Aliyev", "Dauletov",
         "Ergaliyev", "Ismailov", "Kabylov", "Makhambet", "Nurgaliyev", "Otarbayev", "Rysbek",
         "Sabitov", "Toktar", "Ualiev", "Zhakupov", "Abilov", "Beketov", "Esimov", "Ibrayev",
         "Kozhabekov", "Mataev", "Orynbasar", "Sarsenbek", "Tuleuov", "Adilbek", "Bazarbay",
         "Doszhan", "Gabdullin", "Kuandyk", "Muratov", "Ospan", "Rakhmet", "Sultanbek", "Toktarov",
         "Zhandaulet", "Amangeldy", "Baiseitov", "Darkhan", "Erkebulan", "Muslim", "Nurym",
         "Sagyndyk", "Zhaksybai", "Berdibek", "Kenzhebek", "Sapargali", "Tursynbek"]},
}
_GENERIC_NAMES = {
    "male": ["Alex", "Daniel", "Adrian", "Lucas", "David", "Marco", "Victor", "Theo", "Leon", "Felix",
     "Andrei", "Milan", "Ivan", "Diego", "Mateo", "Nikolai", "Pablo", "Stefan", "Tomas", "Rafael",
     "Andres", "Bruno", "Carlos", "Dmitri", "Emilio", "Fernando", "Giovanni", "Hassan", "Igor",
     "Javier", "Karl", "Luca", "Matteo", "Nils", "Omar", "Piotr", "Ravi", "Sergei", "Timo",
     "Umberto", "Vasco", "Wassim", "Youssef", "Zoltan", "Aleksandr", "Bogdan", "Cristian", "Dario",
     "Erik", "Florian", "Gustavo", "Henrik", "Iker", "Janos", "Lorenzo"],
    "female": [
     "Maria", "Sofia", "Elena", "Nina", "Clara", "Ines", "Lena", "Anna", "Mira", "Sara", "Petra",
     "Nadia", "Yara", "Lucia", "Amara", "Freya", "Zara", "Ana", "Elsa", "Marta", "Alba", "Bianca",
     "Camila", "Daniela", "Eva", "Fatima", "Giulia", "Hana", "Irina", "Julia", "Katya", "Mila",
     "Noor", "Olga", "Priya", "Renata", "Tara", "Ulla", "Valeria", "Wanda", "Ximena", "Yuki",
     "Aria", "Bruna", "Chiara", "Greta", "Helena", "Ingrid", "Jana"],
    "last": ["Novak", "Silva", "Kovac", "Costa", "Popov", "Moreau", "Duarte", "Weiss", "Ferrari",
     "Andersen", "Halvorsen", "Bauer", "Marin", "Vidal", "Horvat", "Lindqvist", "Rossi",
     "Fischer", "Nagy", "Sorensen", "Almeida", "Bianchi", "Dubois", "Eriksson", "Fabbri",
     "Georgiev", "Hansen", "Ibrahim", "Jansen", "Kowalski", "Larsen", "Muller", "Nilsson",
     "Oliveira", "Pavlov", "Romano", "Santos", "Tanaka", "Vega", "Wagner", "Abramovic",
     "Bergstrom", "Castillo", "Delgado", "Esposito", "Fernandez", "Gonzalez", "Hoffmann", "Ivanov",
     "Jimenez", "Klein", "Lombardi", "Moretti", "Novikov", "Ortega", "Petrov", "Quiroga", "Reyes",
     "Sokolov", "Torres", "Ustinov", "Vasquez", "Wozniak", "Xiong", "Yamamoto", "Zielinski",
     "Aguilar", "Becker", "Conti", "Dvorak", "Farkas", "Gruber", "Haas", "Ilic", "Jovanovic",
     "Krause", "Lehmann", "Marchetti", "Olsen", "Petersen", "Ricci", "Schneider", "Toth", "Varga",
     "Zaytsev", "Blomqvist", "Cabrera", "Dimitrov", "Fuentes", "Grigoryan", "Hernandez", "Jung",
     "Kaur", "Leroy", "Mikkelsen", "Nakamura", "Okafor", "Petit", "Rahman", "Suzuki", "Traore",
     "Vermeulen", "Yildiz", "Zaman"]}

# Latin America name banks. Some employers hire EXCLUSIVELY LatAm-based talent (e.g. GoFasti:
# "TopTalent from LatAm" / "based in Latin America") and auto-reject anyone outside the region —
# so an etalon for such a role must be a genuinely LatAm person (see `_country_of` + the
# `_LATAM_RESIDENCE_RE` signal), otherwise the honest "based in Latin America? -> No" answer is a
# guaranteed rejection. Spanish-speaking countries share one Spanish bank; Brazil uses Portuguese
# names. Spanish/Portuguese surnames are NOT gendered, so no feminization step is needed.
_LATAM_ES_NAMES = {
    "male": ["Mateo", "Santiago", "Sebastián", "Nicolás", "Diego", "Alejandro", "Samuel",
     "Benjamín", "Emiliano", "Tomás", "Joaquín", "Martín", "Lucas", "Felipe", "Andrés",
     "Bruno", "Ignacio", "Maximiliano", "Agustín", "Facundo", "Bautista", "Cristóbal",
     "Vicente", "Rodrigo", "Julián", "Gael", "Adrián", "Leonardo", "Franco", "Matías",
     "Emanuel", "Álvaro", "Javier", "Gonzalo", "Ramiro", "Esteban", "Camilo", "Fernando"],
    "female": ["Sofía", "Isabella", "Valentina", "Camila", "Valeria", "Mariana", "Gabriela",
     "Daniela", "Victoria", "Martina", "Lucía", "Emilia", "Renata", "Antonella", "Catalina",
     "Julieta", "Fernanda", "Regina", "Ximena", "Micaela", "Guadalupe", "Florencia", "Agustina",
     "Paula", "Constanza", "Josefina", "Antonia", "Carolina", "Natalia", "Andrea", "Manuela",
     "Salomé", "Trinidad", "Mariana", "Amparo", "Rocío", "Pilar", "Bárbara"],
    "last": ["García", "Rodríguez", "Martínez", "López", "González", "Pérez", "Sánchez",
     "Ramírez", "Torres", "Flores", "Rivera", "Gómez", "Díaz", "Cruz", "Morales", "Reyes",
     "Gutiérrez", "Ortiz", "Chávez", "Ramos", "Ruiz", "Vargas", "Castillo", "Jiménez",
     "Mendoza", "Herrera", "Medina", "Aguilar", "Vega", "Rojas", "Molina", "Cáceres",
     "Fuentes", "Cortés", "Delgado", "Guerrero", "Ríos", "Navarro", "Campos", "Peralta",
     "Acosta", "Ibáñez", "Suárez", "Paredes", "Cabrera", "Núñez", "Sandoval", "Bravo"]}
_LATAM_PT_NAMES = {
    "male": ["Miguel", "Arthur", "Heitor", "Bernardo", "Davi", "Théo", "Pedro", "Lorenzo",
     "Matheus", "Rafael", "Enzo", "Gustavo", "João", "Lucas", "Bruno", "Vinícius", "Thiago",
     "Leonardo", "Guilherme", "Rodrigo", "Caio", "Otávio", "Murilo", "André", "Fábio",
     "Ricardo", "Eduardo", "Henrique", "Igor", "Renato", "Marcelo", "Rogério", "Vitor"],
    "female": ["Helena", "Alice", "Laura", "Sophia", "Manuela", "Valentina", "Heloísa", "Luiza",
     "Júlia", "Beatriz", "Marina", "Larissa", "Camila", "Bruna", "Fernanda", "Gabriela",
     "Amanda", "Carolina", "Letícia", "Mariana", "Rafaela", "Isadora", "Bianca", "Patrícia",
     "Renata", "Vitória", "Clara", "Yasmin", "Cristiane", "Aline", "Débora", "Priscila"],
    "last": ["Silva", "Santos", "Oliveira", "Souza", "Lima", "Pereira", "Ferreira", "Almeida",
     "Costa", "Gomes", "Ribeiro", "Martins", "Carvalho", "Rocha", "Araújo", "Barbosa",
     "Nascimento", "Cardoso", "Correia", "Teixeira", "Fernandes", "Moraes", "Cavalcanti",
     "Azevedo", "Melo", "Nunes", "Mendes", "Freitas", "Ramos", "Pinto", "Moreira", "Batista"]}
for _c in ("Mexico", "Colombia", "Argentina", "Chile", "Peru"):
    _NAMES[_c] = _LATAM_ES_NAMES
_NAMES["Brazil"] = _LATAM_PT_NAMES

# Guard: the banks are large and hand-maintained — dedupe each list (order-preserving) so an
# accidental repeat can never skew the random pick or the history-based "no repeat" guarantee.
def _dedup(seq):
    return list(dict.fromkeys(seq))
_NAMES = {c: {g: _dedup(v) for g, v in b.items()} for c, b in _NAMES.items()}
_GENERIC_NAMES = {g: _dedup(v) for g, v in _GENERIC_NAMES.items()}
_US_STATE_BY_CODE = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia",
}


def _us_state_full(token: str) -> str:
    """Resolve a US state code ('TX') or name ('texas') to its full name, else ''."""
    t = (token or "").strip()
    if not t:
        return ""
    if t.upper() in _US_STATE_BY_CODE:
        return _US_STATE_BY_CODE[t.upper()]
    low = t.lower()
    for full in _US_STATE_BY_CODE.values():
        if low == full.lower():
            return full
    return ""


# Major US cities → state, to LOCATE a persona at a remote job's home city so residence screeners
# ("do you reside within 40 miles of <city>?") are answered truthfully.
_US_CITY_STATE = {
    "oklahoma city": "Oklahoma", "tulsa": "Oklahoma", "new york": "New York",
    "los angeles": "California", "chicago": "Illinois", "houston": "Texas", "phoenix": "Arizona",
    "philadelphia": "Pennsylvania", "san antonio": "Texas", "san diego": "California",
    "dallas": "Texas", "austin": "Texas", "denver": "Colorado", "seattle": "Washington",
    "columbus": "Ohio", "charlotte": "North Carolina", "indianapolis": "Indiana",
    "san francisco": "California", "atlanta": "Georgia", "boston": "Massachusetts",
    "nashville": "Tennessee", "detroit": "Michigan", "memphis": "Tennessee", "portland": "Oregon",
    "las vegas": "Nevada", "baltimore": "Maryland", "milwaukee": "Wisconsin", "tucson": "Arizona",
    "fresno": "California", "sacramento": "California", "kansas city": "Missouri", "mesa": "Arizona",
    "omaha": "Nebraska", "raleigh": "North Carolina", "miami": "Florida", "tampa": "Florida",
    "orlando": "Florida", "jacksonville": "Florida", "virginia beach": "Virginia",
    "richmond": "Virginia", "el paso": "Texas", "san juan": "Puerto Rico", "louisville": "Kentucky",
    "albuquerque": "New Mexico", "birmingham": "Alabama", "salt lake city": "Utah",
}


def _job_us_place(job: dict) -> tuple[str, str]:
    """Extract a (city, state) the job is based in from its title/location, so a persona can be
    LOCATED there. Prefers a known US city; falls back to a named US state (city == state name)."""
    text = f"{job.get('title', '')} {job.get('location', '')}".lower()
    for city, state in _US_CITY_STATE.items():
        if city in text:
            return city.title(), state
    for full in _US_STATE_BY_CODE.values():
        if re.search(r"\b" + re.escape(full.lower()) + r"\b", text):
            return full, full
    return "", ""


def _job_bilingual(job: dict) -> bool:
    """True when the role explicitly wants a bilingual (Spanish) speaker — so a synthetic persona
    can be DEFINED as Spanish-bilingual (a persona attribute, like its name/city; not a claim on a
    real person). Kept narrow to Spanish, the dominant US-CSR bilingual pairing."""
    text = f"{job.get('title', '')} {job.get('location', '')}".lower()
    return "bilingual" in text and ("spanish" in text or "bilingual" in (job.get("title") or "").lower())


_CITIES = {
    "United States": ["Austin, TX", "Denver, CO", "Columbus, OH", "Seattle, WA"],
    "Canada": ["Toronto, ON", "Vancouver, BC", "Ottawa, ON", "Calgary, AB"],
    "United Kingdom": ["London", "Manchester", "Bristol", "Leeds"],
    "Kazakhstan": ["Almaty", "Astana", "Shymkent", "Karaganda"],
    "Mexico": ["Mexico City", "Guadalajara", "Monterrey", "Querétaro"],
    "Colombia": ["Bogotá", "Medellín", "Cali", "Barranquilla"],
    "Argentina": ["Buenos Aires", "Córdoba", "Rosario", "Mendoza"],
    "Chile": ["Santiago", "Valparaíso", "Concepción", "Viña del Mar"],
    "Peru": ["Lima", "Arequipa", "Trujillo", "Cusco"],
    "Brazil": ["São Paulo", "Rio de Janeiro", "Belo Horizonte", "Curitiba"],
}


def _country_of(job: dict) -> str:
    """The country to synthesize a persona for. When the location names SEVERAL countries
    (e.g. Salmon's 'Kazakhstan; Kyrgyzstan'), the rule is: if Kazakhstan is one of them use
    Kazakhstan (the agency's own market); otherwise use the FIRST country as it appears in
    the location text. Falls back to the region tag, then United States (the US/CA-remote
    catalog default — an untagged job on a US ATS board is almost always a US company)."""
    loc = job.get("location") or ""
    found = []  # (position_in_text, country)
    for rx, country in _LOC_COUNTRY:
        m = rx.search(loc)
        if m:
            found.append((m.start(), country))
    if found:
        if any(c == "Kazakhstan" for _, c in found):
            return "Kazakhstan"
        found.sort(key=lambda t: t[0])   # else the first country named in the location string
        return found[0][1]
    # location gave no concrete country: a LatAm-exclusive employer needs a LatAm resident
    if _requires_latam(job):
        return random.choice(LATAM_COUNTRIES)
    regions = job.get("regions") or []
    for tag in ("US", "CA", "UK"):
        if tag in regions:
            return _REGION_COUNTRY[tag]
    return _DEFAULT_COUNTRY


def _citizen(country: str) -> str:
    return _CITIZEN.get(country, f"{country} Citizen")


def _extract_obj(text: str) -> dict | None:
    """First balanced {...} JSON object in an LLM reply (tolerates surrounding prose)."""
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except Exception:
                        break
        start = text.find("{", start + 1)
    return None


def _fictional_phone() -> str:
    """A reserved-fiction (555-01xx) number so the persona can never be submitted as real."""
    return f"+1 ({random.randint(200, 989)}) 555-0{random.randint(100, 199)}"


# --- name history (the local LLM is stateless; we keep the memory it lacks) ------------------
# A tiny rolling log of recently-invented demo names so a fresh fill doesn't keep showing the
# same person. Best-effort + gitignored (demo data); any failure never blocks a fill.
_USED_NAMES_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "demo_used_names.json")
_USED_MAX = 400  # remember this many recent names before rolling off the oldest


def _load_used() -> list:
    try:
        with open(_USED_NAMES_PATH) as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _remember_name(name: str) -> None:
    if not name:
        return
    try:
        used = _load_used()
        used.append(name.lower())
        used = used[-_USED_MAX:]
        tmp = _USED_NAMES_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(used, f)
        os.replace(tmp, _USED_NAMES_PATH)  # atomic — no torn file under concurrent clicks
    except Exception:
        pass


def _first_bank(country: str, gender: str) -> list:
    """The first-name list for a country + gender ('male'/'female'); any other gender value
    means 'either' (male+female combined)."""
    banks = _NAMES.get(country, _GENERIC_NAMES)
    if gender == "male":
        return banks["male"]
    if gender == "female":
        return banks["female"]
    return banks["male"] + banks["female"]


def _feminize_kz(surname: str) -> str:
    """A Kazakh/Russian-style surname in its FEMALE form. Kazakh surnames are gendered — a
    woman is 'Sadykova', not 'Sadykov'. Male -ov/-ev/-in take a trailing -a (Sadykov->Sadykova,
    Kaliyev->Kaliyeva, Musin->Musina); the Kazakh -uly ('son of') becomes -kyzy ('daughter of')
    (Nurlanuly->Nurlankyzy). Unmarked stems (-bek/-bay/-khan/-zhan/...) are unisex — unchanged."""
    if surname.endswith("uly"):
        return surname[:-3] + "kyzy"
    if surname.endswith(("ov", "ev", "in")):
        return surname + "a"
    return surname


def _pick_last(country: str, gender: str) -> str:
    """A random surname for the country, feminized for a female Kazakh persona so the surname
    ending matches the (female) first name — the rest of the world's surnames aren't gendered."""
    last = random.choice(_NAMES.get(country, _GENERIC_NAMES)["last"])
    if country == "Kazakhstan" and gender == "female":
        last = _feminize_kz(last)
    return last


def _pick_name(country: str, gender: str = "either") -> str:
    """A random 'First Last' for the country + gender ('male'/'female'/'either'), avoiding
    names used in the recent history so consecutive fills don't show the same person. Falls
    back to a plain random pick if the (bounded) history-avoidance loop is exhausted."""
    first_bank = _first_bank(country, gender)
    used = set(_load_used())
    for _ in range(20):
        full = f"{random.choice(first_bank)} {_pick_last(country, gender)}"
        if full.lower() not in used:
            return full
    return f"{random.choice(first_bank)} {_pick_last(country, gender)}"


def _llm_persona(job: dict, country: str, name: str = "") -> dict | None:
    title = job.get("title", "")
    company = job.get("company", "")
    desc = re.sub(r"\s+", " ", (job.get("description") or "")).strip()[:1200]
    # Pin the name we already chose (see `_pick_name`) so the résumé is authored around it and
    # the model can't drift back to its handful of favourite names.
    name_line = (f"The applicant's name is EXACTLY '{name}'. Use it verbatim as full_name; do "
                 f"NOT invent a different name.\n" if name else "")
    full_name_slot = ('"<given + family name for ' + country + '>"') if not name else f'"{name}"'
    prompt = (
        f"Invent a REALISTIC but entirely FICTIONAL job applicant who is a citizen of and "
        f"resides in {country}, for the role below. This is synthetic demo data — do NOT use "
        f"any real, famous, or celebrity name; make up an ordinary {country} person with a "
        f"city typical of {country}. Tailor the experience to the role.\n" + name_line +
        f"Return ONLY a JSON object, no prose:\n"
        '{"full_name":' + full_name_slot + ','
        '"city":"<a city in ' + country + '>",'
        '"street_address":"<a plausible street address, number + street>",'
        '"phone":"<a ' + country + '-format phone number>",'
        '"years_experience":<int 4-12>,'
        '"headline":"<one-line professional headline>",'
        '"summary":"<2-sentence first-person professional summary>",'
        '"experience":[{"company":"<company>","title":"<title>","dates":"<e.g. 2021-Present>",'
        '"bullets":["<achievement>","<achievement>"]}],'
        '"education":[{"degree":"<e.g. BSc Computer Science>","school":"<university in ' + country + '>",'
        '"field":"<field>","year":"<YYYY>"}],'
        '"skills":["<skill>", "..."]}\n'
        f"Give 3 experience entries (most recent first) and 12-16 skills.\n\n"
        f"ROLE: {title} at {company}\nDESCRIPTION: {desc}")
    for _ in range(2):
        try:
            obj = _extract_obj(_llm_complete(prompt) or "")
        except Exception:
            obj = None
        if obj and obj.get("full_name") and obj.get("experience"):
            return obj
    return None


def _fallback_persona(job: dict, country: str, gender: str = "either") -> dict:
    # Name here is a placeholder — synth_persona overwrites full_name with its own gendered,
    # history-avoided pick — but keep it valid + gender-consistent anyway.
    first_bank = _first_bank(country, gender)
    last_bank = _NAMES.get(country, _GENERIC_NAMES)["last"]
    cities = _CITIES.get(country, [country])
    title = job.get("title") or "Specialist"
    return {
        "full_name": f"{random.choice(first_bank)} {random.choice(last_bank)}",
        "city": random.choice(cities),
        "street_address": f"{random.randint(10, 990)} Main Street",
        "years_experience": random.randint(5, 10),
        "headline": title,
        "summary": (f"Experienced {title} with a track record of delivering results in "
                    f"remote, cross-functional teams."),
        "experience": [
            {"company": "Remote Solutions Inc.", "title": title, "dates": "2021-Present",
             "bullets": [f"Owned {title.lower()} workstreams for a distributed team.",
                         "Improved core delivery metrics through process ownership."]},
            {"company": "Global Services Ltd.", "title": f"Junior {title}", "dates": "2018-2021",
             "bullets": ["Supported day-to-day operations and customer outcomes."]},
        ],
        "education": [{"degree": "BSc Business Administration", "school": "State University",
                       "field": "Business", "year": "2017"}],
        "skills": ["communication", "remote collaboration", "problem solving",
                   "project management", "stakeholder management", "process improvement",
                   "data analysis", "documentation", "prioritization", "time management"],
    }


def _postal(country: str) -> str:
    """A plausible, country-appropriate postal/ZIP for the invented persona so a required
    'Zip Code' field fills with a real value instead of the LLM's 'Not provided' non-answer."""
    c = country or ""
    if c == "United States":
        return f"{random.randint(1000, 99999):05d}"
    if c == "Canada":
        L, D = "ABCEGHJKLMNPRSTVXY", "0123456789"
        return (random.choice(L) + random.choice(D) + random.choice(L) + " "
                + random.choice(D) + random.choice(L) + random.choice(D))
    if c == "United Kingdom":
        L = "ABDEFGHJLNPQRSTUWXYZ"
        return (random.choice(L) + random.choice(L) + str(random.randint(1, 20))
                + " " + str(random.randint(1, 9)) + random.choice(L) + random.choice(L))
    return f"{random.randint(1000, 999999)}"


def _build_candidate(raw: dict, country: str, job: dict) -> dict:
    job_title = job.get("title", "") if job else (raw.get("headline") or "")
    name = str(raw.get("full_name") or "").strip()
    _city_src = str(raw.get("city") or "").strip() or (_CITIES.get(country) or [country])[0]
    _cparts = [p.strip() for p in _city_src.split(",")]
    city = _cparts[0]
    state = ""
    if country == "United States":
        # LOCATE the persona at the job's home city when the posting names one (so residence
        # screeners — "reside within 40 miles of <city>?" — are answered truthfully); else keep a
        # COHERENT (city, state) parsed from the city text, else a bank US city that carries its
        # state (many ATS — Workday/Avature/Oracle — require State).
        jc, js = _job_us_place(job)
        if jc:
            city, state = jc, js
        else:
            state = _us_state_full(_cparts[1]) if len(_cparts) > 1 else ""
            if not state:
                _bank = random.choice(_CITIES["United States"]).split(",")
                city, state = _bank[0].strip(), _us_state_full(_bank[1] if len(_bank) > 1 else "")
    street = str(raw.get("street_address") or "").strip()
    try:
        yoe = int(raw.get("years_experience"))
    except (TypeError, ValueError):
        yoe = random.randint(5, 10)
    skills = [s for s in (raw.get("skills") or []) if isinstance(s, str)]
    exp = []
    for e in (raw.get("experience") or []):
        if isinstance(e, dict):
            exp.append({"company": e.get("company", ""), "title": e.get("title", ""),
                        "dates": e.get("dates", ""), "context": "",
                        "bullets": [b for b in (e.get("bullets") or []) if isinstance(b, str)]})
    edu = []
    for e in (raw.get("education") or []):
        if isinstance(e, dict):
            edu.append({"degree": e.get("degree", ""), "school": e.get("school", ""),
                        "field": e.get("field", ""), "year": str(e.get("year", "") or "")})
    # every DEMO persona gets a numeric suffix -> a UNIQUE mailbox even when the LLM invents
    # the same common name for two jobs (first.last573@takhet.com). Real onboarding via /setup
    # keeps the clean first.last@ (derive_email is unchanged) — the number is demo-only.
    num = random.randint(100, 9999)
    base = derive_email(name)
    if base:
        local, _, dom = base.partition("@")
        email = f"{local}{num}@{dom}"
    else:
        email = ""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "candidate"
    pid = f"demo_{slug}{num}"
    loc = f"{city}, {country}"
    phone = str(raw.get("phone") or "").strip() or _fictional_phone()
    zipc = str(raw.get("zip_code") or raw.get("postal_code") or "").strip() or _postal(country)
    resume = {
        "personal_info": {"name": name, "email": email, "phone": phone, "location": loc,
                          "address": street, "zip_code": zipc},
        "preferred_titles": [job_title], "headline": str(raw.get("headline") or job_title),
        "summary": str(raw.get("summary") or ""), "experience": exp,
        "skills_grouped": {"Skills": skills} if skills else {},
        "certifications": [], "education": edu,
    }
    bilingual = _job_bilingual(job) if job else False
    languages = ["English", "Spanish"] if bilingual else ["English"]
    profile = {
        "id": pid, "full_name": name, "email": email, "phone": phone,
        "location": loc, "city": city, "state": state, "street_address": street,
        "country": country, "zip_code": zipc,
        "linkedin_url": f"https://www.linkedin.com/in/{slug}",
        "work_authorization": _citizen(country), "needs_sponsorship": "No",
        "years_experience": yoe, "is_synthetic": True, "is_sample": True, "resume": resume,
    }
    facts = {"salary_annual": None, "english_level": "Fluent",
             "education_level": "Bachelor's" if edu else "", "tools": skills[:10],
             "languages": languages, "bilingual": bilingual,
             "spanish_level": "Fluent" if bilingual else ""}
    return {"profile": profile, "facts": facts}


def synth_persona(job: dict, gender: str | None = None) -> dict:
    """A fresh, fictional demo candidate whose nationality matches the job's country
    (never a real roster person). LLM-authored with a deterministic fallback.

    `gender` ('male'/'female') comes from the /catalog "Заполнить" M/Ж choice and selects a
    gender-appropriate first name; None (or anything else) rolls a random gender. The chosen
    gender is returned as cand["gender"] (a cache key for ensure_and_wire) AND stored in
    cand["profile"]["sex"] so a demographic gender/pronoun field is answered coherently under the
    persona's own sex (owner policy 2026-08-28) rather than left blocking."""
    country = _country_of(job)
    if gender not in ("male", "female"):
        gender = random.choice(("male", "female"))
    name = _pick_name(country, gender)         # OUR choice, gendered + history-avoided
    raw = _llm_persona(job, country, name)
    if not (raw and str(raw.get("full_name") or "").strip()):
        raw = _fallback_persona(job, country, gender)
    raw["full_name"] = name                     # force the diverse name in BOTH paths
    _remember_name(name)
    cand = _build_candidate(raw, country, job)
    cand["gender"] = gender                      # top-level cache key for ensure_and_wire
    cand["profile"]["sex"] = gender              # persona's assigned sex -> coherent gender/pronoun
    return cand
