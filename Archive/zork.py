"""RETURN TO ZORK: BOOKLY EDITION
West of House... to East Shanbar. Deterministic core, LLM long tail,
and a vulture with limited patience.
Run: python zork.py"""

import os

from dotenv import load_dotenv

load_dotenv()

try:
    from openai import OpenAI
    _client = OpenAI() if os.environ.get("OPENAI_API_KEY") else None
except Exception:
    _client = None

rooms = {
    "westhouse": {
        "desc": ("You are standing in an open field west of a white house, "
                 "with a boarded front door.\nThere is a small mailbox here."),
        "exits": {},
    },
    "storefront": {
        "desc": "Bookly bookstore, East Shanbar. Shelves tower overhead.\nExits: north (fiction), east (counter), west (mill).",
        "exits": {"north": "fiction", "east": "counter", "west": "mill"},
    },
    "fiction": {
        "desc": "Fiction section. A brass LANTERN sits on a shelf.\nExits: south (storefront).",
        "exits": {"south": "storefront"},
        "items": ["lantern"],
    },
    "counter": {
        "desc": "The counter. A GRUE naps behind the register, drooling on a brass KEY.\nExits: west (storefront), down (basement).",
        "exits": {"west": "storefront", "down": "basement"},
        "items": ["key"],
    },
    "basement": {
        "desc": "The basement archive. A locked CHEST sits in the corner.\nExits: up (counter).",
        "exits": {"up": "counter"},
        "dark": True,
    },
    "mill": {
        "desc": ("Boos Miller's mill. The great wheel creaks. BOOS MILLER himself leans\n"
                 "on a barrel, bleary-eyed, holding out a jug.\n"
                 "'Want some rye? Course you do!'\n"
                 "Exits: east (storefront)."),
        "exits": {"east": "storefront"},
    },
}

RIDDLE = ("I speak without a mouth and hear without ears. "
          "I have no body, but I come alive with wind. What am I?")

hints = {
    "westhouse": "There's no vulture here yet. Just you, a boarded house, and a mailbox.",
    "storefront": "The vulture squawks: 'Fiction section. Shiny things. Go NORTH.'",
    "fiction": "The vulture squawks: 'Take the lantern! The basement is DARK, genius.'",
    "counter": "The vulture whispers: 'The key! Take it while the grue snores!'",
    "basement": "The vulture hisses: 'His riddles are ancient. Think: what repeats your words in the mountains?'",
    "mill": "The vulture mutters: 'Humor the drunk. Drink the rye. Trust me.'",
}

loc = "westhouse"
inv = []
morphius_met = False
morphius_gone = False
drank_rye = False
mailbox_open = False
offtrack = 0  # consecutive unrecognized commands; resets on any real command


def game_state():
    r = rooms[loc]
    return (
        f"Location: {loc}. {r['desc']}\n"
        f"Inventory: {', '.join(inv) if inv else 'nothing'}\n"
        f"Morphius met: {morphius_met}, defeated: {morphius_gone}, drank Boos's rye: {drank_rye}"
    )


def llm_narrate(raw_cmd, level):
    """Creative-mode fallback with escalating herding. Narration only -- no state changes."""
    if level == 1:
        steer = ("Roll with the player's silliness playfully, then add ONE gentle one-line "
                 "nudge back toward the quest: recover the lost manuscript of "
                 "'The Winds of Winter' from the chest Morphius sealed.")
    elif level == 2:
        steer = ("The vulture is getting visibly impatient. Answer briefly, then give a firm, "
                 "specific pointer to the player's most useful next step.")
    else:
        steer = ("The vulture pecks the player's shoulder and squawks the current objective "
                 "directly and briefly. Enough fooling around.")
    resp = _client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": (
                "You are the narrator of a 1990s-style text adventure in the Great "
                "Underground Empire (Zork universe). The player's command didn't match "
                "any known game command. Respond in-world, second person, 1-3 sentences. "
                "RULES: narration only -- you cannot move the player, add or remove items, "
                "or change anything about the game. Never solve the player's puzzles; at "
                "most a sly nudge. Wry old-school narrator voice. "
                f"Herding instruction: {steer}"
            )},
            {"role": "user", "content": f"Game state:\n{game_state()}\n\nPlayer typed: {raw_cmd}"},
        ],
    )
    return resp.choices[0].message.content.strip()


def vulture_says(question):
    """The vulture, now with a brain. Rude but useful; never gives riddle answers outright."""
    resp = _client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": (
                "You are a sarcastic vulture companion in a Zork-style text adventure "
                "(Great Underground Empire, Bookly bookstore in East Shanbar). The player "
                "asks for help. Answer in character -- rude but genuinely useful -- in "
                "1-2 sentences. Nudge toward solutions but never give riddle answers outright."
            )},
            {"role": "user", "content": f"Game state:\n{game_state()}\n\nPlayer asks the vulture: {question}"},
        ],
    )
    return resp.choices[0].message.content.strip()


print("RETURN TO ZORK: BOOKLY EDITION")
print("Type anything -- if the parser doesn't know it, the narrator improvises.")
print("But the vulture's patience is limited. Stray too far and it'll herd you back.")
print("Commands: look, go <direction>, take <item>, open mailbox, read letter,")
print("          talk to boos, drink rye, ask vulture [<question>], answer <word>,")
print("          use key, inventory, quit\n")
print(rooms[loc]["desc"])

while True:
    if loc == "basement" and "lantern" not in inv:
        print("\nIt is pitch black. You are likely to be eaten by a grue.")
        print("You were eaten by a grue. THE END.")
        break
    cmd = input("\n> ").strip().lower()
    recognized = True

    if cmd in ("quit", "exit"):
        print("Thanks for playing.")
        break
    elif cmd == "look":
        print(rooms[loc]["desc"])
    elif cmd == "inventory":
        print("You carry:", ", ".join(inv) if inv else "nothing")
    elif cmd == "open mailbox":
        if loc != "westhouse":
            print("There is no mailbox here.")
        elif mailbox_open:
            print("The mailbox is already open.")
        else:
            mailbox_open = True
            rooms["westhouse"].setdefault("items", []).append("letter")
            print("You open the small mailbox. Inside is a letter.")
    elif cmd == "open letter":
        if "letter" in inv:
            print("You're already holding the letter. Try READ LETTER.")
        elif loc == "westhouse" and mailbox_open and "letter" in rooms["westhouse"].get("items", []):
            rooms["westhouse"]["items"].remove("letter")
            inv.append("letter")
            print("You take the letter from the mailbox.")
        elif loc == "westhouse" and not mailbox_open:
            print("The mailbox is closed. Try OPEN MAILBOX first.")
        else:
            print("There is no letter here.")
    elif cmd == "read letter":
        if "letter" in inv:
            print("\n\"CONGRATULATIONS! You have won an all-expenses-paid trip to the")
            print(" Valley of the Sparrows, Great Underground Empire!\"")
            print(" -- the Frobozz Magic Sweepstakes Company\n")
            print("The world spins... you wake up in a bookstore in East Shanbar.")
            print("A vulture lands on your shoulder. It seems to be following you.")
            loc = "storefront"
            print(rooms[loc]["desc"])
        else:
            print("You have nothing to read.")
    elif cmd == "ask vulture":
        print(hints[loc])
    elif cmd.startswith("ask vulture "):
        if _client is None:
            print(hints[loc])
        else:
            try:
                print(vulture_says(cmd[len("ask vulture "):]))
            except Exception:
                print(hints[loc])
    elif cmd == "talk to boos":
        if loc == "mill":
            print("Boos Miller grins through his beard: 'Want some rye? Course you do!")
            print(" Say DRINK RYE and bottoms up!'")
        else:
            print("Boos Miller is at his mill, west of the bookstore.")
    elif cmd == "drink rye":
        if loc != "mill":
            print("There's no rye here. Boos Miller has it at the mill.")
        elif drank_rye:
            print("You've had enough. Even Boos looks concerned.")
        else:
            drank_rye = True
            print("You drink. It burns like dragonfire.")
            print("Boos howls with laughter: 'That's the spirit! The rye'll protect ya --")
            print(" even wizards can't abide the smell!'")
    elif cmd.startswith("go "):
        d = cmd[3:]
        if d in rooms[loc]["exits"]:
            loc = rooms[loc]["exits"][d]
            print(rooms[loc]["desc"])
            if loc == "basement" and "lantern" in inv and not morphius_met:
                morphius_met = True
                print("\nA figure steps from the shadows -- MORPHIUS the wizard!")
                print("'None shall have the manuscript! Answer my riddle or be turned into a newt!'")
                print(f"RIDDLE: {RIDDLE}")
        else:
            print("You can't go that way.")
    elif cmd.startswith("take "):
        item = cmd[5:]
        room_items = rooms[loc].get("items", [])
        if item in room_items:
            if item == "key":
                print("You snatch the key. The grue snores on, undisturbed. Lucky.")
            elif item == "letter":
                print("You take the letter.")
            else:
                print("You take the lantern. It might keep the dark at bay.")
            room_items.remove(item)
            inv.append(item)
        else:
            print("You don't see that here.")
    elif cmd.startswith("answer "):
        if loc != "basement" or not morphius_met or morphius_gone:
            print("There is no riddle to answer here.")
        elif cmd[7:].strip() == "echo":
            morphius_gone = True
            print("MORPHIUS screams and dissolves into mist! The way to the chest is clear.")
        elif drank_rye:
            drank_rye = False
            print("MORPHIUS cackles: 'WRONG!' He raises his staff -- then recoils.")
            print("'Ugh! You reek of Boos Miller's rye!' The spell fizzles into harmless sparks.")
            print("'One more chance, mortal. Choose wisely.'")
        else:
            print("MORPHIUS cackles: 'WRONG!' You are now a newt. THE END.")
            break
    elif cmd in ("use key", "unlock chest", "open chest"):
        if loc != "basement":
            print("Nothing to unlock here.")
        elif not morphius_gone:
            print("MORPHIUS blocks you: 'Answer my riddle first, mortal!'")
        elif "key" in inv:
            print("\nThe key turns. Inside the chest: the lost manuscript of 'The Winds of Winter'!")
            print("Sofia Reyes will be thrilled. Morphius is defeated. YOU WIN.")
            break
        else:
            print("You need a key.")
    else:
        recognized = False
        offtrack += 1
        level = min(offtrack, 3)
        said = None
        if _client is not None:
            try:
                said = llm_narrate(cmd, level)
            except Exception:
                said = None
        if said:
            print(said)
        else:
            print("Try: look, go <direction>, take <item>, open mailbox, read letter,")
            print("     talk to boos, drink rye, ask vulture [<question>], answer <word>,")
            print("     use key, inventory, quit.")

    if recognized:
        offtrack = 0
