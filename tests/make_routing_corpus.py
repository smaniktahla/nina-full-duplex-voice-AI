#!/usr/bin/env python3
"""Writes tests/routing_corpus.jsonl: labeled plain-English requests and the route that should claim each (see live/claims.py).

One line per case: {"text", "route", "ctx"}. route is display | nav | calendar | weather | random | notes | none (none = nobody deterministic claims it: the reply model or
Hermes answers). ctx: route=True means an active route exists, offered=True that Nina just offered to show it, recording=True that a notes recording is running.
Everything here is synthetic (generic places, no household data) and meant to be extended: add a phrase to a list, rerun, commit the jsonl.
    python3 tests/make_routing_corpus.py
"""
import json, os

PLACES = ["Example Beer Garden", "the Riverside Cafe", "Maple Street Pizza", "the airport", "Harbor Point Mall", "Oak Hill Trampoline Park", "12 Elm Street", "Costco"]
NAV = [
    "how far is it to {p}", "how far away is {p}", "how far is {p} from here", "how far to {p}", "what's the distance to {p}", "how many miles to {p}", "how many miles is it to {p}",
    "how many miles away is {p}", "how long to get to {p}", "how long will it take to get to {p}", "how long does it take to get to {p}", "how long would it take to drive to {p}",
    "how long is the drive to {p}", "how long is the drive from here to {p}", "how long till I get to {p}", "how long will it take me to get to {p}", "what's the drive time to {p}",
    "what's the travel time to {p}", "what's the ETA to {p}", "what is the eta to {p}", "give me the eta to {p}", "can you tell me how long it takes to get to {p}",
    "can you look up the travel time to {p}", "look up the drive to {p}", "how's traffic to {p}", "what's traffic like to {p}", "how's the traffic going to {p}",
    "is there traffic on the way to {p}", "give me directions to {p}", "get me directions to {p}", "directions to {p}", "navigate to {p}", "take me to {p}", "route to {p}",
    "how do I get to {p}", "what's the fastest way to get to {p}", "how long to {p}", "how much time to get to {p}", "how many minutes to {p}", "how many minutes away is {p}",
    "is {p} far", "is it far to {p}", "what's the commute to {p}", "hey how long would it take me to drive to {p}", "okay how far is it to {p}",
]
SEND = ["send it to my phone", "send that to my phone", "send the directions to my phone", "text me the directions", "push this to my phone", "put the directions on my phone",
        "can you send this to my phone", "get this on my phone", "send me the route", "send the route to my phone"]
SHOW = ["show me the route on the kitchen screen", "put that on the kitchen display", "show me the route", "show the route", "show me the map", "put the map on the screen",
        "display the route", "bring up the directions", "pull up the route on the kitchen screen", "throw that on the kitchen screen", "can you show me that on the kitchen screen",
        "show it on the screen", "put it on the kitchen display", "show me the directions on the screen", "put the route on the kitchen display", "display the map on the kitchen screen"]
YES = ["yes", "yes show me", "yeah", "yep", "sure", "please", "yes please", "ok", "okay", "go ahead", "do it", "show me", "sure show it", "yes put it up", "yes please show me the route"]
ALT = ["show me the alternate route", "is there another way", "show me another route", "what's the other route", "is there an alternate", "any other routes", "show me a different route",
       "what about the second route", "show the alternative", "what's the alternate route", "show me the alternative route"]
CLOSE = ["close the map", "dismiss the route", "hide the map", "clear the map", "get rid of the map", "take the map down", "close the route", "turn off the map",
         "go back to the normal dashboard", "go back to the dashboards", "back to the dashboards", "resume the dashboards"]
STATE = ["what's on the kitchen screen", "is the route showing on the kitchen screen"]
WEATHER = ["what's the weather", "what's the weather today", "what's the weather tomorrow", "what's the weather like", "how's the weather", "what's the forecast", "what's the forecast for tomorrow",
           "will it rain today", "is it going to rain tomorrow", "is it going to snow", "do I need an umbrella", "do I need a jacket", "how cold is it outside", "how hot is it outside",
           "is it cold outside", "what's the temperature outside", "what's the weather going to be this weekend", "what's the weather this week", "can you look up the weather for tomorrow",
           "hey what's the weather forecast for tomorrow", "what's it like outside", "what's the chance of rain today"]
WEATHER_NONE = ["what location is that weather for", "did you find out where the weather location is coming from", "why is the forecast wrong", "what's the temperature in the living room"]
CALENDAR = ["what's on my calendar today", "what's on my schedule tomorrow", "do I have any meetings today", "what do I have on my schedule for tomorrow", "what's my next appointment",
            "am I busy tomorrow", "when is my dentist appointment", "where is my workout tomorrow", "what time is my meeting", "check my calendar", "what's on my agenda",
            "anything on the calendar this week", "do you know where my workout is", "what is the location for my workout tomorrow morning",
            "can you check my calendar to see where my workout is", "in the meantime what's on my schedule tomorrow", "do I have anything going on this weekend",
            "what's on my calendar friday", "do I have anything on saturday", "what's my schedule next week", "show me tomorrow's schedule", "put the calendar on the kitchen screen",
            "what do I have on wednesday", "am I free thursday", "what's on my agenda for the week", "what's on the calendar for the day after tomorrow"]
RANDOM = ["flip a coin", "toss a coin", "heads or tails", "roll a die", "roll two dice", "roll a d20", "pick a number between 1 and 10", "give me a random number",
          "pick a number from one to a hundred", "choose between pizza and tacos"]
AGENDA_FOLLOWUP = ["and tomorrow", "what about tomorrow", "tomorrow", "how about friday", "what about saturday", "and the day after", "the next day", "and the day after that", "what about yesterday",
                   "the previous day", "how's tomorrow look", "what's tomorrow", "tomorrow's agenda", "show me friday", "and today", "what about next week", "and monday?"]
AGENDA_FOLLOWUP_NONE = ["how about pizza tomorrow", "tomorrow I'm going to the beach with the kids and then to dinner", "play some music tomorrow", "remind me to call mom tomorrow"]
NOTES = ["take notes", "I need you to take notes", "start taking notes", "are you able to create the note in DocMost"]
NOTES_RECORDING = ["write that down", "okay write that down", "that's all", "save the notes"]
NONE = ["what's the capital of France", "tell me a joke", "how long does it take to boil an egg", "how long does a cat live", "how far is the sun", "how far is the moon from earth",
        "how long is a marathon", "how tall is Mount Everest", "what time is it", "who won the game last night", "can you hear me", "hello Nina", "thank you", "okay thanks",
        "I'm gonna go on a walk", "turn on the kitchen light", "how long should I cook chicken", "how far can a drone fly", "what's the distance from earth to mars",
        "how long till Christmas", "how many days until my birthday", "how long is the movie", "how many kilometers is a mile", "what are the directions for making pancakes",
        "how long does it take to learn guitar", "how far did the ball go", "is the sun hot", "what's a good restaurant", "read me the news", "set a timer for ten minutes",
        "what's seventeen times twelve", "play some music", "were you able to find that", "yes", "sure"]


def main():
    rows = []
    add = lambda text, label, **ctx: rows.append({"text": text, "route": label, "ctx": ctx})
    for t in NAV:
        for p in PLACES[:3] if "{p}" in t else [""]:
            add(t.format(p=p), "nav")
    for p in PLACES[3:]:                                           # the rest of the places with a rotating handful of phrasings
        for t in NAV[::5]:
            add(t.format(p=p), "nav")
    for t in SEND:
        add(t, "nav", route=True)
    for t in SHOW:
        add(t, "display", route=True)
    for t in YES:
        add(t, "display", route=True, offered=True)
    for t in ALT + CLOSE + STATE:
        add(t, "display", route=True)
    for t in ["close the map", "hide the map", "go back to the dashboards"]:
        add(t, "display")                                          # closing needs no referent ("there's no route on the screen")
    for t in WEATHER:
        add(t, "weather")
    for t in WEATHER_NONE:
        add(t, "none")
    for t in CALENDAR:
        add(t, "calendar")
    for t in RANDOM:
        add(t, "random")
    for t in AGENDA_FOLLOWUP:
        add(t, "calendar", agenda=True)                             # with an agenda on the screen a day-only request means that day's agenda
    for t in AGENDA_FOLLOWUP_NONE:
        add(t, "none", agenda=True)
    for t in ("and tomorrow", "what about friday", "the next day"):
        add(t, "none")                                              # no agenda showing: not a calendar question on its own
    for t in NOTES:
        add(t, "notes")
    for t in NOTES_RECORDING:
        add(t, "notes", recording=True)
    add("write that down", "none")                                  # nothing is being recorded: not a command
    for t in NONE:
        add(t, "none")
    for t in ("how far is it to sky zone springfield", "how far is it to takoda beer garden", "how many miles to sky zone springfield", "is sky zone springfield far"):
        add(t, "nav")                                               # STT often lowercases proper nouns: a distance question still means a place
    for t in ("how long to sky zone springfield", "how many minutes to sky zone springfield"):
        add(t, "nav")                                               # KNOWN GAP: a lowercase unknown name with a time cue has no place signal (NLR-3: small model)
    add("yes", "none", route=True)                                  # a bare yes with a route but no fresh offer means nothing
    add("sure", "none", route=True)
    add("put that on the kitchen display", "none")                  # no route: 'that' may mean anything
    add("show me the weather", "weather")
    add("show me the pool camera", "none", route=True)
    add("show me a picture of a cat", "none", route=True)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "routing_corpus.jsonl")
    seen, n = set(), 0
    with open(out, "w") as f:
        for r in rows:
            k = (r["text"], json.dumps(r["ctx"], sort_keys=True))
            if k not in seen:
                seen.add(k); f.write(json.dumps(r) + "\n"); n += 1
    print("wrote %d cases to %s" % (n, out))


if __name__ == "__main__":
    main()
