In-Field Planner / AutoDrive Checklist 2 Juni 2026
Voorbereiding DirectSteer / CAN
1. Zorg ervoor dat de TOPCON / AgJunction werken
2. Zet machine buiten, PPP is nodig om te kunnen testen
a. Zorg voor ruimte – je moet een stukje kunnen verplaatsen voordat je GPS
coördinaten / heading krijgt.
3. In Field Controller (van WUR) moet aangesloten worden op Bus #2 van het
display – net zoals we voorheen deden met PeaSense
a. Zie stap 8 voor controle
Display Updaten / Settings goed zetten
4. Maak met de USB een back-up van het display (voor het geval dat de boel
crashed en weer terug moeten)
5. Update het display
6. Ga naar Settings > Machine Options:
a. Zet “INFIELD PLANNING” aan
b. Ga naar Settings > Machine Options > Connect
i. Zet “POG Open Enabled” aan
c. Ga naar Settings > Machine Options > Optional Sensors
i. Zet TENDERNESS aan (indien de sensor er al op zit – voor later)
ii. Zet PEASENSE aan (voor later)
7. Ga naar Settings > Driving
a. Zet Allow AutoDrive AAN
8. Herstart display
Controle CAN
9. Ga naar CAN Monitor → Bus #2
a. Info OXBO_GP6300_AutonomyCanMessages.xlsx
b. Source 0x28 (40) zijn wij zelf. Berichten van belang voor WUR zijn:
i. VEP1 (gps lat / long)
ii. VDS (kompas, groundspeed, altitude, pitch)
iii. DSAP (ankerpunt lat / long – dus het nulpunt)
iv. DSSTAT (huidige richting, PPP ready, autosteer engaged, …)
c. Source 0x1D (29) is de In Field Planner. Berichten van belang zijn:
i. 0xFFCC (staat geen naam bij mij, maar het zou ADJOB moeten zijn).
1. System Active (= PPP ready ontvangen van ons & lijnen
gereed om te streamen(
2. System Run (= ga rijden – doen we nu nog niks mee – je zult
zelf de joystick moeten bedienen!)
3. Huidige Waypoint Index
a. 0 = allereerste
b. Index = punt waar we NAARTOE gaan (niet wat reeds
gepasseerd is!)
4. Totaal aantal waypoints (mag niet meer dan 65.530 zijn!)
5. Job ID (0..65.530)
a. Dit nummer moet veranderen indien we een ander
veld ingaan
b. Verandering van dit nummer in combi met SYSTEM
ACTIVE zorgt ervoor dat de display een nieuwe job
start
ii. 0xFFCD (ADWPI) bevat waypoint informatie
Waypoints doorsturen
d. Bericht heeft info van 1 waypoint elke keer;
i. CM ten oosten van anker nulpunt (‘X”)
ii. CM ten noorden van anker nulpunt (“Y”)
iii. Excel sheet had een fout, stond offset van -25.000, dat moet -
250.000 zijn (25 km) !!! Mogelijk moet WUR dat nog aanpassen
e. Advies is om kort na het setten van een Job, ALLE punten te streamen door
bijvoorbeeld elke 10 msec 1 bericht te sturen
i. Vervolgens kijken of je er 2 of 4 per 10 msec kan doen, dat versneld
het proces aanzienlijk
ii. 65.530 berichten zou dan 655 seconde duren(!)
1. Of een kwart als je 4 berichten per 10 msec kunt pushen
iii. Men mag ook gefaseerd sturen. Dus eerst 100 punten, dan even
pauze (1 seconde), en dan weer een batch.
1. Als de controller even niets stuurt (1 sec), dan (herbouwt) de
display de lijn.
iv. Men mag later eerder verstuurde punten wijzigen – maar
logischerwijs laat je de reeds gepasseerde punten met rust.
Zodra het streamen (even) stopt, zou er een lijn tevoorschijn moeten komen (moet de lijn
uiteraard wel vlakbij de machine zien)
Controleer of de lijn waar je nu “OP” zit iets dikker/feller is. InFieldPlanner geeft het
“current waypoint index” aan, wat wij weer nodig hebben straks om te streamen naar
AgJunction. Dit moet dus veranderen (zie ook Diagnose) naarmate je beweegt.
Indien er NIKS gebeurd:
• Diagnose > In Field Planning
o (hier zijn trouwens ook TENDERNESS en PEASENSE toegevoegd)
- Hebben we PPP (“RTK icoon paars - (FLOAT is niet goed genoeg !!))
o GPS sufficient for AutoDrive staat dan aan
- AutoDrive System actief?
- Total waypoints klopt?
- Allow AutoDrive aan?
- Is het Job ID veranderd / correct?
- Stopt het streamen van waypoints ooit? Zolang deze blijft ratelen, wacht het
scherm
- Controleer Anchor lat / long
o Ontvangt men dezelfde coördinaten als wij?
o Druk op TABEL om meer decimalen te zien
Je kunt ook nog naar de CONSOLE gaan (factory access). Hier zie je DEBUG berichten:
Eerst zou er een “InFieldPlanner – Job ID changed” moeten komen – INDIEN het job
veranderd is. En voor het streamen zie je zoiets. De coördinaten zijn hier in meters (niet
cm) – en zouden dus relatief dichtbij 0 moeten liggen.
Rijden / Sturen
Indien de lijn goed en wel verschijnt, kunnen we een stukje proberen te rijden. LET OP:
- De machine rijdt nog niet vanzelf
o (we doen nog niks met het “RUN” commando – wel kun je ‘m testen in
Diagnose)
o Dus je zult zelf de joystick vooruit moeten zetten
- Het is niet gezegd dat dit werkt met de AgJunction…
- En als de AgJunction de (CURVE) lijn begrijpt, is het nog de vraag of hij niet uit de
bocht vliegt:
o Eerdere testen met curves verliepen… stroef
o LANGZAAM rijden dus (maar ook niet te langzaam, zeg 1 a 2 kph)
o Wellicht de PID regelaar opschroeven om het sturen FEL te krijgen
Je zult een vliegende start moeten maken waarschijnlijk;
- Parkeer 5 meter voor het (1e) lijn punt
- Rij er recht op af, activeer autosteer zodra je het punt bereikt
