# Microsoft 365 Copilot

Every other assistant the harness drives is a program sitting on the machine.
This one is not, and installing something will not change that: **Microsoft 365
Copilot has no command line.** People go looking for one, do not find it, and
conclude the harness is broken. It is not. There is nothing there to find.

What there is, since the middle of 2026, is a web address you can send a
question to. That is what the harness uses.

## The good news, if you have no API keys

You cannot use a key here even if you had one. Microsoft does not allow it: the
only way in is a person signing in. So this fits an organisation that hands out
no keys and never will.

Signing in is a short code you paste into a browser, once. After that the
machine stays signed in and renews itself.

## The three things you need

All three are somebody else's decision. None of them can be worked round from
this end, so the harness says which one is missing rather than shrugging.

**1. A Microsoft 365 Copilot add-on seat.** The Copilot Chat that comes free
with Microsoft 365 is a different product, and Microsoft's own documents say
users without the add-on are not supported. No amount of setting up gets past
this one.

**2. An app registered in your organisation.** A few minutes in the Azure
portal, and free:

- Microsoft Entra ID, then **App registrations**, then **New registration**
- Give it a name. Choose **Accounts in this organizational directory only**
- Under **Authentication**, turn on **Allow public client flows**
- Copy the **Application (client) ID**

**3. An administrator who approves seven permissions**, once, for that app.
Microsoft needs all seven together and refuses if one is missing. They are all
read-only:

```
Sites.Read.All
Mail.Read
People.Read.All
OnlineMeetingTranscript.Read.All
Chat.Read
ChannelMessage.Read.All
ExternalItem.Read.All
```

## Setting it up

Add a route in your settings:

```json
{
  "providers": {
    "microsoft": {
      "kind": "m365-copilot",
      "model": "",
      "microsoft_app": "the Application (client) ID from step 2",
      "time_zone": "Europe/Oslo"
    }
  }
}
```

`microsoft_organisation` is there too, for the Directory (tenant) ID, if your
organisation needs one. Most do not.

Then open **Your team**, put the same Application ID in, and press **Sign in to
Microsoft**. You get a code and an address. Paste one into the other, sign in
with your work account, and the panel notices when you are done.

`time_zone` matters more than it looks: "what meeting do I have tomorrow at
nine" means nothing without it.

## Where the sign-in is kept

With you, not with the project:

```
%LOCALAPPDATA%\NexusHarness\microsoft-sign-in.json
```

A project folder is a thing people copy onto shared drives and push to GitHub,
and this file is the one thing here worth stealing. **Forget this sign-in** in
Your team removes it, which is what to press before handing a machine on.

The file is made with nobody but you allowed in, rather than written first and
locked down afterwards - written first, the token sits there readable by anybody
on the machine for as long as it takes to reach the next line, which is not long
and is long enough. On Windows the harness also asks for the file to be kept to
your account alone, since a file there otherwise takes whatever permissions the
folder hands down.

That is asking, not insisting. **Whoever administers the machine can read it
whatever the harness does.** Treat it the way you would treat being signed in to
Outlook on that machine, because it is the same thing.

## What it will not do

- **Text back, and nothing else.** No writing files, no sending mail, no
  scheduling. Microsoft's limitation, not the harness's.
- **No long jobs.** Anything slow hits their gateway timeout.
- **It searches your work by default.** Every answer is grounded in what you
  personally can see in Microsoft 365, and in the web. It cannot reach anything
  you could not open yourself.

## It is a preview

This runs on Microsoft's `/beta` address. Their own note says `/beta` can change
and is not supported in production. It can stop working one morning through
nobody's fault. When it does, the harness says the address is gone rather than
blaming your sign-in and sending you round in circles.

## When something goes wrong

| What you see | What it means |
| --- | --- |
| Microsoft does not know an app numbered... | Wrong Application ID, or **Allow public client flows** is off |
| ...administrator approves | Nobody has approved the seven permissions yet |
| Allowed the sign-in and would not answer | Either no add-on seat, or the permissions. Microsoft's own words in the same sentence usually say which |
| No Copilot chat at that address | The preview moved or was turned off |
| Asking for fewer questions | Too many at once. Wait a minute |

## What is not tested

Everything on this side of the wire is: the harness asks for exactly what
Microsoft's documents say to ask for, reads back exactly the shape they say
comes back, and has a sentence for each way this can be refused. All of it runs
against a stand-in on the machine.

What no test here can prove is the real thing working, because that needs a
seat, a registered app, and an administrator's approval. The first person with
all three is the first person to find out. If it goes wrong, the message will
say which of the three is missing.
