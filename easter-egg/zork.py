"""RETURN TO ZORK: BOOKLY EDITION
West of House... to East Shanbar. Deterministic core, LLM long tail,
and a vulture with limited patience.
Run: python zork.py  (CLI)
Or:  from zork import ZorkGame  (embeddable engine -- one instance per session)"""

import copy
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

DIRS = {
    "n": "north", "s": "south", "e": "east", "w": "west", "u": "up", "d": "down",
    "north": "north", "south": "south", "east": "east",
    "west": "west", "up": "up", "down": "down",
}

GRUE_LORE = (
    "The grue is a sinister, lurking presence in the dark places of the earth. "
    "Its favorite diet is adventurers, but its insatiable appetite is tempered by "
    "its fear of light. No grue has ever been seen by the light of day, and few "
    "have survived its fearsome jaws to tell the tale."
)

ABOUT_TEXT = (
    "ZORK: Bookly Edition -- an easter egg.\n\n"
    "A playable text adventure hiding behind the Bookly support chat: a boarded-up "
    "white house, a sweepstakes letter, a vulture with limited patience, and a wizard "
    "named Morphius standing between you and the lost manuscript of The Winds of Winter.\n\n"
    "Commands: look, go north/south/east/west/up/down, take lantern/key/letter, "
    "open mailbox, open letter, read letter, talk to boos, drink rye, "
    "ask vulture [question], answer [word], use key, inventory, quit.\n\n"
    "Fair warning: do not enter the basement without the lantern. "
    "You are likely to be eaten by a grue.\n\n"
    "Why it exists: building the Bookly support agent kept taking its maker back to "
    "her Return to Zork days, when half the fun was typing a command into the parser "
    "just to see what it would understand. Back then it was hand-written rules and "
    "imagination. Here it is a deterministic engine for everything that must be exactly "
    "right, with a language model covering the long tail of everything else.\n\n"
    "Ready to play? Type 'open mailbox' to begin or click the book to go back to Paige."
)


class ZorkGame:
    """One self-contained game session. Create one per player; call
    command(text) for each input. Returns (reply_text, game_over)."""

    def __init__(self):
        self.rooms = copy.deepcopy(rooms)  # mutated by play; never share
        self.loc = "westhouse"
        self.inv = []
        self.morphius_met = False
        self.morphius_gone = False
        self.drank_rye = False
        self.mailbox_open = False
        self.offtrack = 0  # consecutive unrecognized commands
        self.over = False
        self.teleported = False

    # -- narration helpers -------------------------------------------------
    def game_state(self):
        r = self.rooms[self.loc]
        exits = ", ".join(f"{d} ({dest})" for d, dest in r["exits"].items()) or "none"
        items = ", ".join(r.get("items", [])) or "none"
        return (
            f"Location: {self.loc}. {r['desc']}\n"
            f"Exits: {exits}\n"
            f"Visible items: {items}\n"
            f"Inventory: {', '.join(self.inv) if self.inv else 'nothing'}\n"
            f"Morphius met: {self.morphius_met}, defeated: {self.morphius_gone}, "
            f"drank Boos's rye: {self.drank_rye}"
        )

    def get_hint(self):
        """State-aware next step. Deterministic, never misleading."""
        if self.loc == "westhouse" and not self.mailbox_open:
            return "The mailbox looks interesting. Try OPEN MAILBOX."
        if self.loc == "westhouse" and "letter" not in self.inv:
            return "Take the letter, then read it: OPEN LETTER, then READ LETTER."
        if self.loc == "westhouse":
            return "Read the letter: READ LETTER."
        if "lantern" not in self.inv:
            return ("You'll need light for dark places. There's a brass lantern north, "
                    "in the fiction section: GO NORTH (or just NORTH), then TAKE LANTERN.")
        if "key" not in self.inv:
            return ("A brass key naps under a grue's nose at the counter, east of the "
                    "storefront. Take it while it snores: TAKE KEY.")
        if not self.drank_rye:
            return ("Boos Miller at the mill (west of the storefront) is offering rye. "
                    "Wizards can't abide the smell. TALK TO BOOS, then DRINK RYE.")
        if not self.morphius_gone:
            return ("Head down into the basement from the counter (GO DOWN) with your "
                    "lantern. Answer Morphius's riddle: ANSWER <word>. "
                    "The vulture's hint: what repeats your words in the mountains?")
        return "Use the key on the chest: USE KEY."

    def llm_narrate(self, raw_cmd, level):
        """Creative-mode fallback with escalating herding. Narration only."""
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
                    "or change anything about the game. The listed exits are the complete "
                    "truth about where the player can go: never claim a listed exit is "
                    "blocked or leads nowhere, and never invent new exits, rooms, items, "
                    "or characters. Never solve the player's puzzles; at "
                    "most a sly nudge. Wry old-school narrator voice. "
                    f"Herding instruction: {steer}"
                )},
                {"role": "user", "content": f"Game state:\n{self.game_state()}\n\nPlayer typed: {raw_cmd}"},
            ],
        )
        return resp.choices[0].message.content.strip()

    def vulture_says(self, question):
        """The vulture, now with a brain. Rude but useful."""
        resp = _client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": (
                    "You are a sarcastic vulture companion in a Zork-style text adventure "
                    "(Great Underground Empire, Bookly bookstore in East Shanbar). The player "
                    "asks for help. Answer in character -- rude but genuinely useful -- in "
                    "1-2 sentences. Nudge toward solutions but never give riddle answers outright."
                )},
                {"role": "user", "content": f"Game state:\n{self.game_state()}\n\nPlayer asks the vulture: {question}"},
            ],
        )
        return resp.choices[0].message.content.strip()

    # -- core ----------------------------------------------------------------
    def opening(self):
        return self.rooms[self.loc]["desc"]

    def command(self, raw_cmd):
        """Run one player command. Returns (reply_text, game_over)."""
        out = []

        def p(*a):
            out.append(" ".join(str(x) for x in a))

        if self.over:
            return "The game is over. Restart to play again.", True

        # Grue check runs before every command, as in the original loop.
        if self.loc == "basement" and "lantern" not in self.inv:
            p("\nIt is pitch black. You are likely to be eaten by a grue.")
            p("You were eaten by a grue.")
            p(GRUE_LORE)
            p("THE END. Click RESTART to respawn.")
            self.over = True
            return "\n".join(out), True

        cmd = (raw_cmd or "").strip().lower()
        recognized = True
        game_over = False

        # Forgiving verb aliases: "pick up lantern" == "take lantern".
        for _pre, _repl in (("pick up ", "take "), ("pick ", "take "),
                            ("get ", "take "), ("grab ", "take ")):
            if cmd.startswith(_pre):
                cmd = _repl + cmd[len(_pre):]
                break

        if cmd in ("quit", "exit"):
            p("Thanks for playing.")
            game_over = True
        elif cmd == "about":
            p(ABOUT_TEXT)
        elif cmd in ("help", "?"):
            p("Commands: LOOK, EXAMINE GRUE, GO <north|south|east|west|up|down> (or just NORTH, S, E...), "
              "TAKE <item>, OPEN MAILBOX, OPEN LETTER, READ LETTER, TALK TO BOOS, DRINK RYE, "
              "ASK VULTURE [question], ANSWER <word>, USE KEY, INVENTORY, HINT, ABOUT, QUIT.")
        elif cmd == "hint":
            p(self.get_hint())
        elif cmd == "look":
            p(self.rooms[self.loc]["desc"])
        elif cmd in ("examine grue", "look at grue", "inspect grue"):
            if self.loc == "counter":
                p(GRUE_LORE)
                p("This one naps behind the register, drooling on a brass key. Best not to wake it.")
            elif self.loc == "basement":
                p(GRUE_LORE)
                p("And it is pitch black in here. You should leave. Now.")
            else:
                p("There is no grue here. Be glad.")
        elif cmd == "inventory":
            p("You carry:", ", ".join(self.inv) if self.inv else "nothing")
        elif cmd == "open mailbox":
            if self.loc != "westhouse":
                p("There is no mailbox here.")
            elif self.mailbox_open:
                p("The mailbox is already open.")
            else:
                self.mailbox_open = True
                self.rooms["westhouse"].setdefault("items", []).append("letter")
                p("You open the small mailbox. Inside is a letter.")
        elif cmd == "open letter":
            if "letter" in self.inv:
                p("You're already holding the letter. Try READ LETTER.")
            elif (self.loc == "westhouse" and self.mailbox_open
                    and "letter" in self.rooms["westhouse"].get("items", [])):
                self.rooms["westhouse"]["items"].remove("letter")
                self.inv.append("letter")
                p("You take the letter from the mailbox.")
            elif self.loc == "westhouse" and not self.mailbox_open:
                p("The mailbox is closed. Try OPEN MAILBOX first.")
            else:
                p("There is no letter here.")
        elif cmd == "read letter":
            if "letter" not in self.inv:
                p("You have nothing to read.")
            elif not self.teleported:
                self.teleported = True
                p("\n\"CONGRATULATIONS! You have won an all-expenses-paid trip to the")
                p(" Valley of the Sparrows, Great Underground Empire!\"")
                p(" -- the Frobozz Magic Sweepstakes Company\n")
                p("The world spins... you wake up in a bookstore in East Shanbar.")
                p("A vulture lands on your shoulder. It seems to be following you.")
                self.loc = "storefront"
                p(self.rooms[self.loc]["desc"])
            else:
                p("\"CONGRATULATIONS! ...\" -- the Frobozz Magic Sweepstakes Company")
                p("(You've already followed the letter here. The vulture preens.)")
        elif cmd == "ask vulture":
            p(hints[self.loc])
        elif cmd.startswith("ask vulture "):
            if _client is None:
                p(hints[self.loc])
            else:
                try:
                    p(self.vulture_says(cmd[len("ask vulture "):]))
                except Exception:
                    p(hints[self.loc])
        elif cmd == "talk to boos":
            if self.loc == "mill":
                p("Boos Miller grins through his beard: 'Want some rye? Course you do!")
                p(" Say DRINK RYE and bottoms up!'")
            else:
                p("Boos Miller is at his mill, west of the bookstore.")
        elif cmd == "drink rye":
            if self.loc != "mill":
                p("There's no rye here. Boos Miller has it at the mill.")
            elif self.drank_rye:
                p("You've had enough. Even Boos looks concerned.")
            else:
                self.drank_rye = True
                p("You drink. It burns like dragonfire.")
                p("Boos howls with laughter: 'That's the spirit! The rye'll protect ya --")
                p(" even wizards can't abide the smell!'")
        elif cmd.startswith("go ") or cmd in DIRS:
            raw_d = cmd[3:] if cmd.startswith("go ") else cmd
            d = DIRS.get(raw_d, raw_d)
            if d in self.rooms[self.loc]["exits"]:
                self.loc = self.rooms[self.loc]["exits"][d]
                p(self.rooms[self.loc]["desc"])
                if self.loc == "basement" and "lantern" in self.inv and not self.morphius_met:
                    self.morphius_met = True
                    p("\nA figure steps from the shadows -- MORPHIUS the wizard!")
                    p("'None shall have the manuscript! Answer my riddle or be turned into a newt!'")
                    p(f"RIDDLE: {RIDDLE}")
            else:
                p("You can't go that way.")
        elif cmd.startswith("take "):
            item = cmd[5:]
            room_items = self.rooms[self.loc].get("items", [])
            if item in room_items:
                if item == "key":
                    p("You snatch the key. The grue snores on, undisturbed. Lucky.")
                elif item == "letter":
                    p("You take the letter.")
                else:
                    p("You take the lantern. It might keep the dark at bay.")
                room_items.remove(item)
                self.inv.append(item)
            else:
                p("You don't see that here.")
        elif cmd.startswith("answer "):
            if self.loc != "basement" or not self.morphius_met or self.morphius_gone:
                p("There is no riddle to answer here.")
            elif cmd[7:].strip() == "echo":
                self.morphius_gone = True
                p("MORPHIUS screams and dissolves into mist! The way to the chest is clear.")
            elif self.drank_rye:
                self.drank_rye = False
                p("MORPHIUS cackles: 'WRONG!' He raises his staff -- then recoils.")
                p("'Ugh! You reek of Boos Miller's rye!' The spell fizzles into harmless sparks.")
                p("'One more chance, mortal. Choose wisely.'")
            else:
                p("MORPHIUS cackles: 'WRONG!' You are now a newt. THE END. Click RESTART to respawn.")
                game_over = True
        elif cmd in ("use key", "unlock chest", "open chest"):
            if self.loc != "basement":
                p("Nothing to unlock here.")
            elif not self.morphius_gone:
                p("MORPHIUS blocks you: 'Answer my riddle first, mortal!'")
            elif "key" in self.inv:
                p("\nThe key turns. Inside the chest: the lost manuscript of 'The Winds of Winter'!")
                p("Sofia Reyes will be thrilled. Morphius is defeated. YOU WIN.")
                game_over = True
            else:
                p("You need a key.")
        else:
            recognized = False
            self.offtrack += 1
            level = min(self.offtrack, 3)
            said = None
            if _client is not None:
                try:
                    said = self.llm_narrate(cmd, level)
                except Exception:
                    said = None
            if said:
                p(said)
            else:
                p("Try: look, go <direction> (or just north, s, e...), take <item>,")
                p("     open mailbox, read letter, talk to boos, drink rye,")
                p("     ask vulture [<question>], answer <word>, use key,")
                p("     inventory, hint, about, quit.")

        if recognized:
            self.offtrack = 0
        if game_over:
            self.over = True
        return "\n".join(out), self.over


def main():
    print("RETURN TO ZORK: BOOKLY EDITION")
    print("Type anything -- if the parser doesn't know it, the narrator improvises.")
    print("But the vulture's patience is limited. Stray too far and it'll herd you back.")
    print("Commands: look, go <direction>, take <item>, open mailbox, read letter,")
    print("          talk to boos, drink rye, ask vulture [<question>], answer <word>,")
    print("          use key, inventory, quit\n")
    g = ZorkGame()
    print(g.opening())
    while True:
        try:
            cmd = input("\n> ")
        except EOFError:
            break
        text, over = g.command(cmd)
        print(text)
        if over:
            break


if __name__ == "__main__":
    main()
