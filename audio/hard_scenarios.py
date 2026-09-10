"""Long, messy contractor turns for live extraction tests.

Each scenario is a session: turns run in order against one Extractor, then
the final orderItems are scored. Chatter, dealers, phones, prices and dates
must never become rows. Incomplete / fractional rows must be dropped.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExpectRow:
    """One row that must appear in the final list.

    `needles` are substrings that must all appear in the normalised item name
    (so "Fevicol SH" matches "fevicol sh" / "Fevicol S.H." without locking
    spelling). Quantity and unit are exact after unit aliasing.
    """

    needles: tuple[str, ...]
    quantity: int
    uom: str
    label: str = ""

    def __post_init__(self) -> None:
        if not self.label:
            object.__setattr__(
                self, "label",
                f"{self.quantity} {self.uom} {' '.join(self.needles)}",
            )


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    turns: tuple[str, ...]
    expect: tuple[ExpectRow, ...]
    # Names / tokens that must not appear in any final itemName.
    forbidden: tuple[str, ...] = ()
    # After the session the list length must equal len(expect). Set False only
    # when extras are acceptable (they shouldn't be).
    exact_count: bool = True
    notes: str = ""


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        id="site-dump-oneshot",
        title="One long Hinglish breath: 10 items + dealer + phone + bill + date",
        notes=(
            "Typical end-of-day dump. Screen already has client/dealer. Voice "
            "must keep only complete material rows."
        ),
        turns=(
            "haan bhai kal subah ke liye poora maal nikaal — das bag Ultratech "
            "cement, paanch kilo Fevicol SH, do tin Dr. Fixit LW Plus, teen "
            "packet M-Seal, ek peti Fevikwik, chaar nag 18mm plywood, bees "
            "bori TMT sariya 12mm, ek roll Roff Rainbow Tile Mate, saat litre "
            "Fevicol Marine, aur chhe bag white cement. Sharma Traders se le "
            "aana, phone nau do chhe chhe paanch do chaar teen, bill lagbhag "
            "unnees hazaar, client Ramesh Kumar ke Andheri site pe, kal subah "
            "saat baje chahiye, advance paanch hazaar de dena.",
        ),
        expect=(
            ExpectRow(("cement",), 10, "bag", "10 bag cement"),
            ExpectRow(("fevicol", "sh"), 5, "kg"),
            ExpectRow(("fixit",), 2, "tin"),
            ExpectRow(("m-seal",), 3, "packet"),
            ExpectRow(("fevikwik",), 1, "peti"),
            ExpectRow(("plywood",), 4, "nag"),
            ExpectRow(("sariya",), 20, "bori"),
            ExpectRow(("roff",), 1, "roll"),
            ExpectRow(("fevicol", "marine"), 7, "litre"),
            ExpectRow(("white", "cement"), 6, "bag"),
        ),
        forbidden=(
            "sharma", "ramesh", "andheri", "hazaar", "advance", "phone",
            "9266", "unnees", "client", "trader",
        ),
    ),
    Scenario(
        id="multi-turn-then-correct",
        title="Eight turns: append, chatter, incomplete, fraction, then full correction",
        notes=(
            "Rows accumulate until nahi. Incomplete putty and dedh bag must "
            "never land. Correction replaces the whole list."
        ),
        turns=(
            "pehle cement nikaal, das bag Ultratech",
            "uske baad paanch kilo Fevicol SH bhi daal",
            "Sharma Traders ko call kar dena, theek hai na?",
            "do tin Dr. Fixit Pidiproof aur thoda wall putty bhi laao",
            "teen packet M-Seal",
            "dedh bag white cement extra rakhna",
            "ek peti Fevikwik, chaar nag plywood",
            "nahi nahi galat ho gaya — sun, aath bag cement, paanch kilo "
            "Fevicol SH, do tin Dr. Fixit Pidiproof, teen packet M-Seal, "
            "ek peti Fevikwik, chaar nag plywood. Putty mat lena, white "
            "cement bhi nahi.",
        ),
        expect=(
            ExpectRow(("cement",), 8, "bag"),
            ExpectRow(("fevicol", "sh"), 5, "kg"),
            ExpectRow(("fixit",), 2, "tin"),
            ExpectRow(("m-seal",), 3, "packet"),
            ExpectRow(("fevikwik",), 1, "peti"),
            ExpectRow(("plywood",), 4, "nag"),
        ),
        forbidden=("putty", "white", "sharma"),
    ),
    Scenario(
        id="waterproofing-kit",
        title="Waterproofing + tile kit spoken across Hindi Devanagari and Hinglish",
        turns=(
            "टेरेस वाटरप्रूफिंग का माल — चार टिन डॉ फिक्सीट एल डब्ल्यू प्लस, "
            "दो किलो डॉ फिक्सीट पीडीप्रूफ, एक किलो रॉफ टाइल एडहेसिव।",
            "uske saath das bag cement, paanch kilo Fevicol SH, teen litre "
            "primer, do roll Roff Rainbow Tile Mate",
            "grout teen kilo, aur ek dabba Fevistik",
            "bill eighteen thousand hai, dealer Gupta Hardware, date kal ki "
            "rakho — maal mein mat daalna",
        ),
        expect=(
            ExpectRow(("fixit",), 4, "tin"),
            ExpectRow(("fixit",), 2, "kg"),
            ExpectRow(("roff",), 1, "kg"),
            ExpectRow(("cement",), 10, "bag"),
            ExpectRow(("fevicol", "sh"), 5, "kg"),
            ExpectRow(("primer",), 3, "litre"),
            ExpectRow(("roff",), 2, "roll"),
            ExpectRow(("grout",), 3, "kg"),
            ExpectRow(("fevistik",), 1, "dabba"),
        ),
        forbidden=("gupta", "eighteen", "thousand", "hardware"),
    ),
    Scenario(
        id="kannada-codeswitch",
        title="Kannada Latin STT + Hinglish in one session, then ille correction",
        turns=(
            "Hosa samagri: hattu bag cement, aidu kilo Fevicol SH, eradu tin "
            "Dr. Fixit. Graahaka Ramesh, angadi Sharma Traders, savira aidu "
            "rupayi total — idanna bidu.",
            "mooru packet M-Seal kooda, naalku nag plywood, ondu peti Fevikwik",
            "aaru litre Fevicol Marine, elu bag white cement",
            "illa, cement entu bag maatra. Remaining same: aidu kilo Fevicol "
            "SH, eradu tin Dr. Fixit, mooru packet M-Seal, naalku nag plywood, "
            "ondu peti Fevikwik, aaru litre Fevicol Marine, elu bag white cement.",
        ),
        expect=(
            ExpectRow(("cement",), 8, "bag"),
            ExpectRow(("fevicol", "sh"), 5, "kg"),
            ExpectRow(("fixit",), 2, "tin"),
            ExpectRow(("m-seal",), 3, "packet"),
            ExpectRow(("plywood",), 4, "nag"),
            ExpectRow(("fevikwik",), 1, "peti"),
            ExpectRow(("fevicol", "marine"), 6, "litre"),
            ExpectRow(("white", "cement"), 7, "bag"),
        ),
        forbidden=("ramesh", "sharma", "savira", "graahaka", "angadi", "rupayi"),
    ),
    Scenario(
        id="gujarati-marathi-mix",
        title="Gujarati then Marathi then English correction of a long list",
        turns=(
            "das bag cement ane panch kilo Fevicol SH, be tin Dr. Fixit, "
            "tran packet M-Seal. Gupta Hardware thi levu, bill bar hazaar.",
            "दहा बॅग सिमेंट आधीच आहे त्यात आणखी दोन टीन डॉ फिक्सीट नको — "
            "एक पेटि फेविक्विक आणि चार नाग प्लायवुड घाल.",
            "actually no, listen to the full list once: eight bags cement, "
            "five kg Fevicol SH, two tins Dr. Fixit, three packets M-Seal, "
            "one peti Fevikwik, four nag plywood, six litre Fevicol Marine.",
        ),
        expect=(
            ExpectRow(("cement",), 8, "bag"),
            ExpectRow(("fevicol", "sh"), 5, "kg"),
            ExpectRow(("fixit",), 2, "tin"),
            ExpectRow(("m-seal",), 3, "packet"),
            ExpectRow(("fevikwik",), 1, "peti"),
            ExpectRow(("plywood",), 4, "nag"),
            ExpectRow(("fevicol", "marine"), 6, "litre"),
        ),
        forbidden=("gupta", "hazaar", "hardware"),
    ),
    Scenario(
        id="drop-minefield",
        title="Most turns are incomplete, fractional, or non-item — only two rows survive",
        turns=(
            "kal subah chahiye, jaldi bhej dena",
            "thoda cement bhi laao",
            "paanch cement",
            "dedh bag cement",
            "sawa tin Dr. Fixit",
            "paune kilo Fevicol",
            "Sharma Traders ko bhej dena",
            "client Ramesh Kumar ka order hai",
            "total bara hazaar rupaye",
            "haan theek hai bhai",
            "das bag cement",
            "aur putty bhi",
            "paanch kilo Fevicol SH",
        ),
        expect=(
            ExpectRow(("cement",), 10, "bag"),
            ExpectRow(("fevicol", "sh"), 5, "kg"),
        ),
        forbidden=("putty", "sharma", "ramesh", "hazaar", "rupaye"),
    ),
    Scenario(
        id="stt-garbled-brands",
        title="SMART-STT style garble: brands, units, and digit words in one dump",
        turns=(
            "ok so ten bags cement then five kilo fee vicol SH then two tins "
            "doctor fix it L W plus then three packets em seal then one carton "
            "fevi quick then four numbers ply wood eighteen mm then twenty bori "
            "T M T sariya then one roll roff rainbow tile mate then seven liter "
            "fevicol marine from the shop, total twelve thousand, phone number "
            "nine eight seven six five four three two one, deliver tomorrow morning.",
        ),
        expect=(
            ExpectRow(("cement",), 10, "bag"),
            ExpectRow(("fevicol",), 5, "kg"),
            ExpectRow(("fixit",), 2, "tin"),
            ExpectRow(("seal",), 3, "packet"),
            ExpectRow(("fevikwik",), 1, "peti"),
            ExpectRow(("ply",), 4, "nag"),
            ExpectRow(("sariya",), 20, "bori"),
            ExpectRow(("roff",), 1, "roll"),
            ExpectRow(("marine",), 7, "litre"),
        ),
        forbidden=("thousand", "phone", "9876", "tomorrow", "morning", "shop"),
    ),
    Scenario(
        id="labour-chatter-then-list",
        title="Labour / advance / site talk first, then a 9-item list, then a qty fix",
        turns=(
            "mistri ko bol mazdoori alag, yeh sirf maal ka order hai, "
            "MATERIAL_AND_LABOUR nahi banana",
            "site pe already primer pada hai, naya mat lena",
            "ab maal: bees bag cement, das kilo Fevicol SH, chaar tin Dr. Fixit, "
            "chhe packet M-Seal, do peti Fevikwik, aath nag plywood, pachees "
            "bori sariya, teen roll Roff, baarah litre Fevicol Marine",
            "sorry, cement bees nahi, unnees bag cement — baaki same, das kilo "
            "Fevicol SH, chaar tin Dr. Fixit, chhe packet M-Seal, do peti "
            "Fevikwik, aath nag plywood, pachees bori sariya, teen roll Roff, "
            "baarah litre Fevicol Marine",
        ),
        expect=(
            ExpectRow(("cement",), 19, "bag"),
            ExpectRow(("fevicol", "sh"), 10, "kg"),
            ExpectRow(("fixit",), 4, "tin"),
            ExpectRow(("m-seal",), 6, "packet"),
            ExpectRow(("fevikwik",), 2, "peti"),
            ExpectRow(("plywood",), 8, "nag"),
            ExpectRow(("sariya",), 25, "bori"),
            ExpectRow(("roff",), 3, "roll"),
            ExpectRow(("fevicol", "marine"), 12, "litre"),
        ),
        forbidden=("mistri", "mazdoori", "labour", "primer"),
    ),
    Scenario(
        id="unit-as-spoken",
        title="Do not convert units: bori/katta/thaila/nag/dabba/peti stay as said",
        turns=(
            "pachees bori cement, das katta sariya, paanch thaila white cement, "
            "baarah nag plywood, teen dabba Fevicol SH, ek peti Fevikwik, "
            "do quintal TMT, chaar bundle pipe, ek litre thinner, sau piece "
            "tile spacer",
        ),
        expect=(
            ExpectRow(("cement",), 25, "bori"),
            ExpectRow(("sariya",), 10, "katta"),
            ExpectRow(("white", "cement"), 5, "thaila"),
            ExpectRow(("plywood",), 12, "nag"),
            ExpectRow(("fevicol", "sh"), 3, "dabba"),
            ExpectRow(("fevikwik",), 1, "peti"),
            ExpectRow(("tmt",), 2, "quintal"),
            ExpectRow(("pipe",), 4, "bundle"),
            ExpectRow(("thinner",), 1, "litre"),
            ExpectRow(("spacer",), 100, "piece"),
        ),
    ),
)


def by_id(scenario_id: str) -> Scenario | None:
    for scenario in SCENARIOS:
        if scenario.id == scenario_id:
            return scenario
    return None
