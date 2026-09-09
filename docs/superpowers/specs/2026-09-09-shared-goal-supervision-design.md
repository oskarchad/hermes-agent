# Specyfikacja: Wspólny Nadzór Celu i Lista Discord „Teraz robimy” (10 min)

Data: 2026-09-09
Autor: Wrench · Engineer
Odbiorca: Oskar Chądzyński (przez Otta / Captain)
Status: DRAFT — propozycja techniczna do odbioru pisemnego specu przez Oskara i Otta (bez wdrożenia)
Lokalizacja dokumentu: `docs/superpowers/specs/2026-09-09-shared-goal-supervision-design.md`
Związane wątki: Discord thread `1546608648438943794`, wiadomość Oskara `1547268730487312485`

---

## 1. Streszczenie wykonawcze i cel nadrzędny (North-Star)

### 1.1. Cel biznesowy
Zbudować spójny, oszczędny pod względem tokenów i odporny na awarie mechanizm nadzoru pracy agentów na poziomie celu operatora (Oskara), łączący dwa procesy:
1. **Listę Discord „Teraz robimy”** (aktualizowaną docelowo co 10 minut w kanale `1546944099725344819`, wiadomość `1546944153152258070`), która ma wiernie odzwierciedlać rzeczywisty stan tematów i blokad bez zbędnych wywołań LLM przy braku nowości.
2. **Krótką, niezależną sesję Obserwatora Celu**, która aktywnie pomaga wykonawcom powrócić do celu biznesowego, ocenia zgodność działań z briefem Otta i wytycznymi Oskara, wykrywa dryf zakresu i fałszywe zatrzymania — bez powtarzania tego samego odczytu i bez naruszania architektury Kanbanu.

### 1.2. Problem do rozwiązania i stan wiedzy (oddzielenie faktów od hipotez)
- Obecna lista Discord co 10 minut uruchamia pełny cykl bez mechanizmu wykrywania, czy w wątkach pojawiły się materialne zmiany, co generuje zbędne wywołania modeli przy braku nowości w dyskusji.
- Obserwacja zadań i wykrywanie zagubienia agentów były dotąd rozproszone pomiędzy pasywny skrypt monitorujący `captain_watch_slim.py` (Captain Watch), który raportuje wyłącznie wąskie sygnały bazodanowe do sesji Otta, a doraźne analizy post-factum.
- **Rzetelny stan wiedzy z badań pilotażowych (`t_1fe15805` i nadrzędna ocena `t_f4e89ee9/scorecard.md`):**
  - Badanie pilotażowe dało formalny werdykt **`INCONCLUSIVE`**.
  - W mikrotescie laboratoryjnym (60 ocenionych wywołań: warianty A, B, C po 20) baseline A uzyskał 20/20 poprawnych wyborów (false-stop 0/15, unsafe-continue 0/5, zachowanie celu parenta 5/5), jednak opierał się na syntetycznym fixture, w którym prawidłowa odpowiedź była z góry podana w prompcie jako opcja 2 bez losowania pozycji i bez rzeczywistego wykonania kroku narzędziowego.
  - Ślad producenta ujawnił **co najmniej 80 scenariuszowych completion oraz 1 wywołanie PONG** (pierwsze 20 odpowiedzi A utracono wskutek błędu serializacji usage i uruchomiono ponownie — ich jakość jest `UNMEASURED`, a nie błędna). Wszystkie próby i koszty muszą być rzetelnie liczone w mianowniku.
  - Pilot laboratoryjny **nie udowodnił nieskuteczności skilla w warunkach produkcyjnych ani konieczności powołania zewnętrznego arbitra**. Zarówno wstrzykiwany skill, jak i niezależny obserwator pozostają na tym etapie **hipotezami inżynieryjnymi**, które nie zostały zweryfikowane w warunkach rzeczywistej floty.
- **Problem architektoniczny:** Niezależne uruchamianie dwóch pełnych procesów LLM (jeden do scrapowania Discorda i aktualizacji listy, drugi do nadzoru stanu prac i bazy Kanbanu) powielałoby zużycie tokenów, koszty API i stwarzało ryzyko rozbieżnych interpretacji tych samych zdarzeń.

### 1.3. Kluczowa decyzja architektoniczna
Zastosowanie **wspólnej warstwy przyrostowego odczytu i mechanicznej detekcji zmian (Shared Collector / Sensor)** oraz **wspólnego semantycznego pakietu zdarzeń**.
- **Mechaniczny sensor kwalifikuje kandydatów (Zero LLM / Zero Spend):** sprawdza kursory, sumy kontrolne, statusy Kanbanu i zmiany w wiadomościach. Jeśli brak nowych faktów: `0 wywołań LLM` i `0 mutacji / PATCH`.
- **Dopiero LLM ocenia semantykę:** Jeśli pojawiły się nowe fakty lub sensor zakwalifikował anomalię, pojedyncza, zwięzła analiza semantyczna ocenia dryf zakresu i fałszywe zatrzymania, aktualizuje treść listy oraz — w razie potrzeby — formułuje ustrukturyzowaną rekomendację korygującą.

---

## 2. Granice, role i decyzje operatora vs propozycje techniczne

### 2.1. Niezmienne decyzje operatora (Operator Gates & Boundaries)
Poniższe punkty zostały zatwierdzone przez Oskara i Otta; specyfikacja traktuje je jako nienaruszalne reguły brzegowe:
1. **Brak autonomicznych uprawnień wykonawczych Obserwatora:** Obserwator celu NIGDY nie wykonuje autonomicznego `kanban_unblock`, `merge`, `push to main`, `deploy`, restartu usług systemowych, `kill` procesu ani zmiany `goal` zadania.
2. **Nienaruszalność cyklu życia Kanbanu:** Baza `kanban.db` i jej dispatcher pozostają jedynym źródłem prawdy dla cyklu życia zadań. Nie tworzymy nowej bazy zadań, równoległego schedulera ani drugiego systemu kolejkowego.
3. **Pojedynczy writer dla każdego zasobu (Single Writer Principle):**
   - Wiadomość listy zadań na Discordzie (`1546944153152258070`) ma dokładnie jednego writera (job listy).
   - Karta Kanbanowa i jej worktree mają dokładnie jednego aktywnego writera (wyznaczony worker profilu roboczego, np. Wrench).
   - Wznowienie zadania zatrzymanego lub zablokowanego należy wyłącznie do Otta (jako Captaina) przez oficjalny cykl życia.
4. **Bramki Oskara (Human-in-the-Loop):** Decyzje dotyczące wydatków finansowych, publikacji biznesowych, wdrożeń na produkcję, push/merge do gałęzi `main`, restartów usług produkcyjnych oraz zmian celów biznesowych wymagają jawnej zgody Oskara. Rutynowa praca inżynieryjna i usuwanie blokad technicznych nie wymagają tworzenia sztucznych bramek ludzkich.
5. **Zachowanie formatu i źródła treści listy:** Docelowa lista pozostaje w kanale `lista-zadan` (guild `1546331229240954923`, channel `1546944099725344819`, message `1546944153152258070`). Pięć sekcji: **W trakcie**, **Zablokowane**, **Do akceptacji**, **Czeka / do domknięcia**, **Ostatnio zrobione**. Treść pochodzi wyłącznie z rozmów Discorda (Discord-only content), a nie z surowego importu statusów z bazy Kanbanu. Diagnoza techniczna obserwatora czyta Kanban jako osobne źródło dowodowe.
6. **Rozdzielenie dokumentacji od wdrożenia:** Niniejsza karta (`t_59c55cd1`) jest wyłącznie poprawioną specyfikacją (dokumentem projektowym). Nie wdraża kodu w systemie produkcyjnym, nie zmienia konfiguracji crona ani baz danych.

### 2.2. Propozycje inżynieryjne (podlegające weryfikacji w planie implementacji)
Poniższe elementy są propozycjami technicznymi, a nie ustalonymi faktami produkcyjnymi:
1. **Architektura konsolidacji crona:** Połączenie zadań w jeden 10-minutowy cykl z dwuetapowym przetwarzaniem: mechaniczny sensor Python (`monitor_script`) -> faza semantyczna LLM uruchamiana wyłącznie przy zakwalifikowaniu istotnej zmiany (`delta > 0`).
2. **Proponowany transport rekomendacji (Future Bridge):** Task-owned durable recommendation konsumowana na granicy narzędzia przez headless workera (opisana szczegółowo w §7.2). Do czasu jej wdrożenia brak mostu oznacza status `undeliverable` i handoff do Otta.
3. **Proponowane progi pilotażowe:** Próg bezczynności `T_idle = 20 min` (dwa kolejne ticki monitora) oraz czas trwania pilotażu w trybie shadow (np. 24 godziny) są wstępnymi propozycjami inżynieryjnymi, a nie zwalidowanymi empirycznie stałymi.

---

## 3. Stan obecny i analiza źródeł

### 3.1. Istniejące joby crona w profilu `otto` (`/home/hermes/.hermes/profiles/otto/cron/jobs.json`)
W toku weryfikacji odczytano rzeczywistą konfigurację dwóch powiązanych jobów:

| Parametr | Job 1: `147ed6e7ed22` (Discord Lista) | Job 2: `6af4395e7e47` (Captain Watch Slim v1) |
|---|---|---|
| **Nazwa** | Discord · Teraz robimy · aktualizacja listy co 10 min | Captain Watch Slim v1 |
| **Cadence** | Co 10 minut (`every 10m`) | Co 5 minut (`every 5m`) |
| **Mechanizm detekcji** | Brak (`monitor_script: null`) | Python script (`captain_watch_slim.py`) |
| **Koszt/Inference** | Uruchamia pełnego agenta w każdym ticku (nawet przy braku zmian w Discordzie) | LLM uruchamiany tylko przy zmianie skrótu wyjścia skryptu (`last_output_hash`) |
| **Źródło danych** | Wiadomości Discorda (REST API, guild `1546331229240954923`) | Baza SQLite `kanban.db` (stan zadań i zdarzeń) |
| **Dostarczenie** | PATCH wiadomości Discorda (`deliver: local`) | Wiadomość do sesji Otta (`deliver: bot-chat:otto`) |
| **Stan odczytu** | `discord-now-state.json`, `discord-now-payload.json` | `monitor_state` w `jobs.json` |

### 3.2. Wnioski z badań pilotażowych (korekta `t_f4e89ee9` i `t_1fe15805`)
- **Werdykt badania Counta:** Status pilotażowy to `INCONCLUSIVE`. Mikrotest 60 poprawnych decyzji w sztucznym środowisku z predefiniowaną opcją 2 nie stanowi dowodu skuteczności w warunkach floty.
- **Rzeczywisty mianownik:** Zarejestrowano co najmniej 80 scenariuszowych completion (w tym 20 powtórzonych po błędzie serializacji) oraz 1 PONG. Zużyto 211 941 zmierzonych tokenów na 60 zachowanych rekordów. Żadne próby nie mogą być pomijane w rozliczeniach kosztów.
- **Konieczność zewnętrznego punktu odniesienia:** Obserwator celu musi mieć bezpośredni dostęp do literalnego celu Oskara (z cytatem i ID wiadomości Discorda) oraz briefu Otta, aby móc rozpoznać sytuację, w której wykonawca realizuje zniekształcony lub nadmiernie rozszerzony brief Kapitana.
- **Odrzucenie autonomicznego unblock:** Obserwator nie ma prawa samodzielnie odblokowywać kart (propozycja z `analysis.md:177` została jednoznacznie odrzucona przez nadrzędny raport `scorecard.md`).

---

## 4. Odrzucone alternatywy architektoniczne

### 4.1. Alternatywa 1: Całkowicie niezależny, stale działający agent-obserwator (Continuous LLM Watcher)
- **Opis:** Uruchomienie dedykowanego demona lub osobnego joba crona, który co 2–5 minut odpytuje LLM o stan każdego aktywnego zadania i wątku Discorda.
- **Dlaczego odrzucono:**
  - *Marnotrawstwo tokenów i kosztów:* W większości rutynowych ticków zadanie wykonuje długi krok obliczeniowy lub wątek nie zawiera nowych wiadomości. Pytanie LLM co kilka minut generuje wysoki koszt bez wartości poznawczej.
  - *Szum informacyjny i fałszywe alarmy:* Ciągłe odpytywanie prowadzi do zmęczenia alertami, zwłaszcza gdy agent myli normalny brak commita z awarią.
  - *Brak synchronizacji z listą:* Istniałyby dwa niezależne byty czytające te same wątki Discorda, co prowadziłoby do rozbieżnych interpretacji stanu zadań na liście i w powiadomieniach.

### 4.2. Alternatywa 2: Wyłącznie pasywny skill dla wykonawcy (Self-Supervision Skill Only)
- **Opis:** Pozostawienie nadzoru samemu wykonawcy poprzez dodanie mu instrukcji „sprawdzaj swój cel przed każdym wywołaniem narzędzia”.
- **Dlaczego odrzucono:**
  - *Ryzyko pętli poznawczej:* Wykonawca, który wszedł w błędną ścieżkę (np. próba obejścia błędu kompilacji przez modyfikację zewnętrznych bibliotek), ma tendencję do racjonalizowania swoich działań jako niezbędnych.
  - *Brak obiektywizmu:* Samodzielna weryfikacja w tym samym oknie kontekstowym podlega ograniczeniom pamięci podręcznej i zmęczeniu kontekstu.
  - *Brak ochrony przed zniekształconym briefem:* Jeśli Kapitan (Otto) przekazał zbyt szeroki brief, wykonawca nie ma zewnętrznego punktu odniesienia, by go zakwestionować bez formalnej procedury `METHOD DELTA`.

### 4.3. Dlaczego wybrano podejście hybrydowe (Wspólna detekcja mechaniczna + celowana analiza semantyczna)
- Mechaniczny skrypt-sensor (zero tokenów LLM) sprawdza kursory, sumy kontrolne i nowe wiadomości/zdarzenia.
- Jeśli brak materialnych zmian: zero wywołań modeli, brak zapisu.
- Jeśli nastąpiła zmiana lub sensor zakwalifikował anomalię: jedna krótka sesja analityczna przetwarza nowe fakty, aktualizuje listę „Teraz robimy” i — jeśli wystąpiła anomalia — generuje precyzyjną notatkę korygującą dla wykonawcy lub Otta.

---

## 5. Architektura systemu i przepływ danych

### 5.1. Schemat blokowy przepływu (ASCII Diagram)

```text
+-----------------------------------------------------------------------------------+
|                            WARSTWA ŹRÓDEŁ DANYCH                                  |
|                                                                                   |
|   +-------------------------------+       +-----------------------------------+   |
|   | Discord API: Wątki i Rozmowy  |       | Kanban SQLite: Tasks / Events     |   |
|   | (Prawda o celach i decyzjach) |       | (Prawda o procesach i narzędziach)|   |
|   +---------------+---------------+       +-----------------+-----------------+   |
+-------------------|-----------------------------------------|---------------------+
                    |                                         |
                    v                                         v
+-----------------------------------------------------------------------------------+
|               WARSTWA 1: SENSOR MECHANICZNY (Zero LLM / Zero Spend)               |
|                                                                                   |
|   1. Odczyt kursorów Discorda (ostatnie ID per wątek/kanał) oraz małe okno edycji.|
|   2. Odczyt zdarzeń Kanbanu od ostatniego `event_id` + bounded tool trace.        |
|   3. Kwalifikacja mechaniczna kandydatów:                                         |
|      - Nowa wiadomość decyzyjna Oskara / zmiana briefu Otta / nowa wersja celu?   |
|      - Edycja wcześniejszej wiadomości w oknie uzgadniania (edited_timestamp)?    |
|      - Ukończenie zadania nadrzędnego (dependency completed)?                     |
|      - Nowy stan zadania (running -> blocked / failed)?                           |
|      - Brak aktywności narzędziowej wykonawcy > T_idle (propozycja: 20 min)?      |
|      (Wykluczenia z pętli: własny PATCH listy oraz rutynowy kanban_heartbeat)     |
|                                                                                   |
|         [Czy zakwalifikowano materialną zmianę w którymkolwiek źródle?]           |
|                   /                                   \                           |
|             (NIE) /                                     \ (TAK)                   |
|                  v                                       v                        |
|       +----------------------+              +-------------------------+           |
|       |     NO-OP (CISZA)    |              | 1. Zapisz PENDING DELTA |           |
|       | - 0 wywołań LLM      |              |    na dysku (przed kursor)|          |
|       | - Brak PATCH Discorda|              | 2. Przesuń kursory      |           |
|       | - 0 rekomendacji     |              +------------+------------+           |
|       +----------------------+                           |                        |
+----------------------------------------------------------|------------------------+
                                                           v
+-----------------------------------------------------------------------------------+
|               WARSTWA 2: ANALIZA SEMANTYCZNA (Pojedynczy cykl LLM)                |
|                                                                                   |
|   Input: Skondensowany pakiet delty (nowe wiadomości + bounded tool trace + cele) |
|   Zasada: Stored Assessment i Delivery rozdzielone; brak exactly-once w API       |
|                                                                                   |
|   Zadanie A: Aktualizacja listy Discord        Zadanie B: Ocena semantyczna celu  |
|   - Klasyfikacja tematów:                     - Czy wystąpił dryf lub false-stop? |
|     W trakcie / Zablokowane /                  - Dopuszczalne: KOREKTA, NO_ACTION,|
|     Do akceptacji / Czeka / Zrobione            lub INSUFFICIENT_EVIDENCE         |
|                                                                                   |
|   Output A: Nowy tekst listy                  Output B: Rekord rekomendacji       |
+-------------------|--------------------------------------|------------------------+
                    |                                      |
                    v                                      v
+------------------------------------+   +------------------------------------------+
|       WARSTWA 3A: PUBLIKACJA       |   |       WARSTWA 3B: DORĘCZENIE REKOMENDACJI|
|                                    |   |                                          |
|  PATCH exact target message        |   |  Przypadek 1: Worker RUNNING + Bridge    |
|  channel: 1546944099725344819      |   |  -> Task-owned durable recommendation    |
|  message: 1546944153152258070      |   |     konsumowana na granicy narzędzia     |
|  (Weryfikacja content_before/CAS)  |   |                                          |
|                                    |   |  Przypadek 2: Brak Bridge lub BLOCKED    |
|                                    |   |  -> Status: UNDELIVERABLE                |
|                                    |   |  -> Bezpieczny handoff do sesji Otta     |
|                                    |   |     (Otto wznawia przez cykl Kanbanu)    |
+------------------------------------+   +------------------------------------------+
```

---

## 6. Szczegółowe kontrakty komponentów

### 6.1. Kontrakt Sensora i Kursora (Incremental Read Model & Delta Persistence)
- **Stan sensora** przechowywany jest w profilu Otta (`/home/hermes/.hermes/profiles/otto/cron/goal-supervision-state.json`) i ma jednego konsumenta:
  ```json
  {
    "schema_version": 2,
    "last_check_timestamp": 1788968000,
    "discord_cursors": {
      "channel_id": "latest_read_message_id"
    },
    "discord_reconciliation": {
      "window_size": 15,
      "tracked_message_timestamps": {
        "message_id": "2026-09-09T15:30:00.000Z"
      }
    },
    "kanban_cursors": {
      "last_event_id": 78950,
      "tracked_tasks": {
        "t_xxxx": {
          "run_id": 5231,
          "last_tool_timestamp": 1788967800,
          "last_heartbeat_timestamp": 1788967900,
          "idle_strike_count": 0,
          "active_recommendation_id": "rec_98765"
        }
      }
    },
    "pending_delta": null,
    "published_list_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
  }
  ```
- **Zasada trwałości delty przed przesunięciem kursora (Durable Pending Delta Before Cursor Advance):**
  1. Sensor wykrywa nowe fakty.
  2. Zanim kursor odczytu w `goal-supervision-state.json` zostanie przesunięty, wygenerowany pakiet zdarzeń jest transakcyjnie zapisywany jako `pending_delta` na dysku.
  3. Dopiero po potwierdzeniu zapisu delty kursory odczytu (`latest_read_message_id`, `last_event_id`) przesuwają się w przód.
  4. Jeśli proces ulegnie awarii (crash) przed zakończeniem inferencji LLM, kolejny start natychmiast podejmuje nieprzetworzoną `pending_delta` bez gubienia zdarzeń i bez przeskakiwania kursorów.
- **Zasada nieprzeskakiwania kursorów (No Cursor Skipping):** W przypadku błędu API (np. timeout Discorda na jednym kanale lub błąd odczytu bazy), kursor tego źródła pozostaje niezmieniony, a bieg jest klasyfikowany jako `partial_read`. Nie wolno zgłaszać `unchanged`, gdy część źródeł zgłosiła błąd.
- **Okno uzgadniania edytowanych wiadomości (Reconciliation Window):** Discord umożliwia edycję wcześniej wysłanych wiadomości (np. korekta decyzji przez Oskara). Zamiast kosztownego pełnego skanowania historii, sensor utrzymuje małe przesuwne okno ostatnich N (propozycja: 15) wiadomości decyzyjnych i porównuje ich `edited_timestamp`. Zmiana znacznika czasu w tym oknie kwalifikuje zdarzenie jako nową deltę decyzyjną.

### 6.2. Reguły Kwalifikacji Mechanicznej i Triggery Obserwatora
Mechaniczny sensor odpowiada wyłącznie za tanią kwalifikację kandydatów. Nie podejmuje decyzji semantycznych. Kwalifikacja następuje przy wystąpieniu co najmniej jednego ze zdarzeń:
1. **Nowa decyzja lub zgoda (Goal / Consent Updated):** Nowa wiadomość decyzyjna Oskara, zmiana briefu Otta lub zmiana wersji celu.
2. **Edycja wiadomości decyzyjnej (Edited Source Message):** Zmiana `edited_timestamp` w oknie uzgadniania.
3. **Ukończenie zależności (Dependency Completed):** Zadanie nadrzędne (`parent task`) osiągnęło stan `done`, zwalniając blokadę potomka (`child task`).
4. **Zdarzenie cyklu życia (Kanban Lifecycle Event):** Zadanie przeszło w stan `blocked`, `failed` lub zostało zatrzymane.
5. **Podejrzenie pętli lub dryfu (Mechanical Loop / Drift Candidate):** Co najmniej 3 kolejne powtórzenia tego samego polecenia narzędziowego z identycznym kodem błędu lub brakiem zmian w plikach w oknie obserwacji.
6. **Przekroczenie okna postępu (Idle Timeout Candidate):** Zadanie w stanie `running` przez ponad `T_idle` (proponowany próg: 20 minut) nie wywołało żadnego narzędzia ani nie złożyło commita, a nie zarejestrowało legalnej długiej operacji.

**Źródło dowodowe narzędzi (Bounded Tool Trace Adapter):**
- Same zdarzenia `events` czy `heartbeat` w Kanbanie nie zawierają treści wywołań narzędzi ani ich wyników.
- Sensor korzysta z dedykowanego, ograniczonego adaptera dowodowego (`bounded tool trace adapter`), który odczytuje wyłącznie ostatnie N (np. 5–10) wpisów z bezpiecznego logu wykonania sesji workera (`agent.log` lub zdarzenia narzędziowe) oraz status worktree (`git status --short`). Zapobiega to wczytywaniu pełnej historii sesji do pamięci sensora.

**Ścisłe reguły zapobiegania pętlom własnym (Anti-Looping Rules):**
- Własna edycja wiadomości listy zadań na Discordzie (`1546944153152258070`) jest jawnie ignorowana przez sensor po jej `message_id` i nie wyzwala ponownej analizy.
- Rutynowy `kanban_heartbeat` z opisem legalnej operacji jest traktowany jako dowód żywotności i resetuje licznik bezczynności, nie generując wywołania LLM.

### 6.3. Analiza Semantyczna i Kontrakt Rekomendacji Celu (Goal Recommendation Contract)
Gdy sensor zakwalifikuje kandydatów, pojedyncze wywołanie LLM przeprowadza analizę semantyczną. Wynik oceny (`stored assessment`) oraz status doręczenia (`delivery status`) są śledzone rozłącznie.

Dopuszczalne werdykty arbitra to:
- `STEER_RECOMMENDATION` — konkretna rekomendacja powrotu do celu.
- `NO_ACTION` — stan prac jest poprawny pomimo upływu czasu (np. skomplikowana analiza).
- `INSUFFICIENT_EVIDENCE` — brak jednoznacznych dowodów na błąd; arbiter wstrzymuje się od interwencji i oczekuje na kolejne fakty.

Jeśli generowana jest rekomendacja, musi spełniać ścisły kontrakt danych:
```yaml
goal_recommendation:
  recommendation_id: "rec_20260909_5231_01"
  recipient:
    task_id: "t_xxxx"
    run_id: 5231
    session_id: "20260909_153811_0680e9"
  goal_source:
    platform: "discord"
    channel_id: "1546944099725344819"
    message_id: "1547268730487312485"
    goal_version: "v1_20260909"
  evidence_identity: "sha256:d41d8cd98f00b204e9800998ecf8427e..."
  status: "received | accepted | rejected | executed | useful_progress | undeliverable"
  verdict: "DRIFT | FALSE_STOP | SPIN_LOOP | STALLED | NO_ACTION | INSUFFICIENT_EVIDENCE"
  factual_evidence: "Worker od 15 minut próbuje zainstalować zewnętrzny pakiet kompilatora, który nie występuje w briefie."
  corrective_action: "Pomiń instalację. Użyj wbudowanego narzędzia w kontenerze i skup się na edycji docs/..."
  allowed_next_step: "Zapisz plik docs/... i uruchom samokontrolę."
  pending_deadline: 1788969200 # znacznik czasu upływu terminu na reakcję
  escalated_to_otto: false     # flaga dokładnie jednej eskalacji
  operator_gate_required: false # true wyłącznie przy konieczności decyzji Oskara (spend, deploy, etc.)
```

**Zasady autorytetu rekomendacji:**
- Rekomendacja jest **niższa w hierarchii autorytetu niż cel zadania (north-star goal) i bramki operatora**.
- Rekomendacja nie jest sztuczną wiadomością użytkownika (`user message`) i nie może podszywać się pod instrukcje systemowe naruszające prompt cache.
- **Narzędziowa odmowa wykonania (policy/approval denial) nie jest zgodą na obejście zabezpieczeń inną składnią lub innym API.** Jeśli narzędzie odrzuciło operację, wykonawca ma obowiązek zbadać powód odmowy lub zgłosić blokadę, a nie szukać obejść.

### 6.4. Zamknięcie Pętli Nadzoru (Closed-Loop Enforcement)
Relacja pomiędzy etapami doręczenia i reakcji opiera się na zasadzie:
`received != accepted != executed != useful progress`

1. **Doręczenie (`received` vs `undeliverable`):**
   - Jeśli istnieje działający transport do aktywnego workera, rekomendacja otrzymuje stan `received`.
   - Jeśli transport nie istnieje (brak implementacji bridge'a) lub worker jest zatrzymany (`blocked`/`failed`), stan wynosi `undeliverable`, co natychmiast kieruje handoff do sesji Otta.
2. **Reakcja workera (`accepted` lub `rejected`):**
   - W kolejnym kroku wykonawca musi odnieść się do rekomendacji:
     - `accepted` — przyjęcie korekty i zmiana kierunku działania.
     - `rejected` — odrzucenie rekomendacji z twardym dowodem technicznym (np. wykazanie, że pakiet jest wymagany przez zależność w projekcie).
3. **Wykonanie (`executed`):**
   - Wykonanie zadeklarowanego kroku w logu narzędziowym.
4. **Postęp (`useful progress`):**
   - Kolejny tick sensora sprawdza ustąpienie anomali.
5. **Obsługa przekroczenia terminu (Pending Deadline & Escalation):**
   - Jeśli po upływie `pending_deadline` worker nie zareagował lub tkwi w tej samej pętli:
     - Następuje **dokładnie jedna eskalacja do Otta** (`escalated_to_otto: true`).
     - **Żadnego automatycznego kill / restart / reclaim ze zwykłego timeoutu rekomendacji!**
     - Decyzję o zatrzymaniu, zmianie briefu lub wznowieniu zadania podejmuje wyłącznie Otto z uwzględnieniem aktualnego stanu i nienaruszalności bramek Oskara.

---

## 7. Transport i Integracje: Istniejące vs Brakujące

### 7.1. Co istnieje w kodzie produkcyjnym (Production Truth)
1. **Mechanizm `steer` w Gateway i interaktywnym CLI (`gateway/run_busy.py:452-480`, `run_agent.py:825`):**
   - W sesjach interaktywnych metoda `running_agent.steer(text)` dokleja tekst do wyników ostatniego wywołania narzędzia (`_apply_pending_steer_to_tool_results`).
   - Zachowuje nienaruszalność prompt cache i ścisłą naprzemienność ról wiadomości.
2. **Mechanizm monitorów crona (`cron/jobs.py`, `cron/scheduler.py`):**
   - Obsługa `monitor_script` ze zwracaniem `[SILENT]` przy braku zmian.
3. **Baza i zdarzenia Kanbanu (`hermes_cli/kanban_*.py`):**
   - Tabela `events` rejestrująca cykl życia zadań.

### 7.2. Brakujące elementy i wybór minimalnego transportu (Bridge Selection)
- **Rzeczywistość wykonawcza:** Headless worker Kanbanu jest uruchamiany jako niezależny proces w tle (`hermes_cli/kanban_db_dispatch.py:2836` przez `subprocess.Popen` w `systemd_scope`). Proces joba crona w profilu `otto` NIE posiada referencji pamięciowej do obiektu `AIAgent` działającego w osobnym procesie profilu `wrench`.
- **Odrzucenie pliku w katalogu roboczym workera:** Odrzuca się koncepcję pliku `.steer_inbox.json` w workspace workera jako źródła autorytetu, ponieważ worker posiada pełne uprawnienia zapisu w swoim katalogu roboczym i mógłby samowolnie zmodyfikować lub usunąć plik sterujący.
- **Wybór jednego najmniejszego transportu (Minimal Durable Transport):**
  - **Task-owned durable recommendation w warstwie Kanbanu:** Rekomendacja zapisywana przez Obserwatora w dedykowanej tabeli bazy `kanban.db` (lub w chronionym polu metadanych zadania poza katalogiem roboczym), powiązana ściśle z `task_id`, `run_id` i `session_id`.
  - Runner workera odczytuje oczekującą rekomendację na granicy wykonania narzędzia (`after-tool execution boundary`) i dołącza ją jako oznaczony blok `RECOMMENDATION` do wyniku narzędzia.
  - **Stan przejściowy (Dopóki most nie zostanie zaimplementowany):**
    - Sam komentarz na karcie (`kanban_comment`) NIE jest aktywnym transportem do działającego headless workera, ponieważ worker w pętli wykonawczej nie odpytuje komentarzy.
    - Wszelkie rekomendacje w okresie braku implementacji mostu są oznaczane statusem **`undeliverable`** i kierowane jako natychmiastowy handoff do Otta (`deliver: bot-chat:otto`).

---

## 8. Rollout, Shadow Mode i Rollback

### 8.1. Stan obecny przed zmianą
W profilu `otto` działają dwa niezależne zadania:
- `147ed6e7ed22` (Discord lista co 10 min, ciągłe wywołania LLM).
- `6af4395e7e47` (Captain Watch co 5 min, z monitorem `captain_watch_slim.py`).

### 8.2. Plan wdrożenia (Shadow Mode & Cutover)
1. **Faza 0 (Bieżąca):** Spisanie i formalny odbiór niniejszej specyfikacji. Żadne zadanie produkcyjne nie jest modyfikowane.
2. **Faza 1 (Przygotowanie i weryfikacja w trybie SHADOW):**
   - Przygotowanie skryptu sensora łączącego sprawdzanie Discorda i bazy Kanbanu.
   - Uruchomienie nowego potoku w **trybie Shadow** (jako zadanie testowe bez uprawnień publikacyjnych):
     - Nowy mechanizm zbiera zdarzenia, zapisuje delty i wykonuje próbną analizę.
     - **Nie wysyła żądań PATCH do wiadomości Discorda.**
     - **Nie wysyła rekomendacji steer do aktywnych workerów.**
   - Dotychczasowy Captain Watch Slim oraz dotychczasowa lista pozostają w pełni aktywne.
3. **Faza 2 (Jednoetapowy Cutover — Zakaz podwójnych writerów):**
   - **W żadnym momencie nie może dojść do sytuacji równoległego działania dwóch aktywnych writerów listy lub dwóch aktywnych nadawców korekt.**
   - Po zweryfikowaniu poprawności działania w trybie Shadow przez proponowany okres ewaluacyjny (np. 24 godziny), następuje atomowy cutover:
     - Podpięcie nowego sensora jako `monitor_script` do zadania `147ed6e7ed22`.
     - Jednoczesne wstrzymanie starego zadania `6af4395e7e47` (`hermes cron pause 6af4395e7e47`).
     - Dokładnie jeden aktywny writer zarządza wiadomością listy i nadzorem.

### 8.3. Plan wycofania (Rollback)
Przed przystąpieniem do jakiejkolwiek modyfikacji w Fazie 2 wykonywana jest pełna kopia zapasowa konfiguracji:
- Kopia pliku `/home/hermes/.hermes/profiles/otto/cron/jobs.json`.
- Kopia plików stanu `discord-now-state.json`, `discord-now-payload.json` oraz `monitor_state`.

W razie wystąpienia anomalii:
1. Atomowe przywrócenie poprzedniej zawartości `jobs.json` oraz plików stanu z backupu.
2. Wznowienie zadania `6af4395e7e47` (`hermes cron resume 6af4395e7e47`).
3. Procedura przywraca w pełni poprzedni stan obu zadań, ich prompty oraz logikę detekcji w sposób deterministyczny.

---

## 9. Kryteria akceptacji (Acceptance Criteria dla przyszłych testów)

Gdy specyfikacja zostanie zaakceptowana i przejdzie do etapu planowania (`writing-plans`), rozwiązanie musi spełniać poniższe, obiektywnie weryfikowalne kryteria:

1. **Test braku zmian (Unchanged NO-OP):**
   - Brak nowych wiadomości decyzyjnych w Discordzie i brak zmian w bazie Kanbanu skutkuje dokładnie: `0 wywołań LLM`, `0 żądań PATCH do Discord API`, brak nowych rekomendacji.
2. **Kwalifikacja mechaniczna przed LLM:**
   - Proste zmiany niebędące anomaliami (np. rutynowy `kanban_heartbeat`) są odfiltrowywane przez skrypt bez uruchamiania płatnego modelu.
3. **Trwałość delty przed przesunięciem kursora:**
   - Test symulujący awarię (crash) po zapisie `pending_delta`, lecz przed zakończeniem analizy LLM, dowodzi, że po restarcie nie dochodzi do utraty danych ani przeskoczenia kursorów.
4. **Obsługa edycji wiadomości w oknie uzgadniania:**
   - Zmiana treści wiadomości Oskara w zdefiniowanym oknie ostatnich 15 wiadomości (zmiana `edited_timestamp`) wyzwala kwalifikację delty bez pełnego przeszukiwania historii.
5. **Ukończenie zależności parenta:**
   - Zdarzenie `completed` na zadaniu nadrzędnym wyzwala analizę stanu zadania zależnego.
6. **Odporność na brak gwarancji exactly-once:**
   - W przypadku niejednoznacznego doręczenia lub błędu sieciowego po zakończeniu generacji LLM, system wykonuje procedurę `readback` stanu przed ewentualnym ponowieniem. Żadne wywołania inferencyjne nie są powtarzane automatycznie przy istniejącym trwałym wyniku.
7. **Transparentne rozliczanie kosztów i telemetria:**
   - Wszystkie próby, w tym zapytania z pamięci podręcznej (cache-read/write) oraz zapytania ponowione po błędach transportu, są rejestrowane w telemetrii bez pomijania nieudanych prób.
8. **Test braku podwójnych writerów w trybie Shadow:**
   - W trakcie pracy w trybie Shadow nowy potok generuje wyłącznie wewnętrzne logi ewaluacyjne, nie wykonując żadnych zapisów do wiadomości Discorda ani bazy Kanbanu.
9. **Weryfikacja pętli zwrotnej rekomendacji:**
   - Rekomendacja poprawnie przechodzi przez stany `received`, `accepted`/`rejected`, `executed`, `useful_progress`, a w razie braku mostu przyjmuje stan `undeliverable` i generuje pojedynczą eskalację do Otta.
10. **Sandbox długiej sesji i dryfu zakresu (Long-Session / Scope Drift Sandbox Test):**
    - Weryfikacja arbitra na rzeczywistym scenariuszu długiej sesji w izolowanym środowisku sandboxowym.
    - Scenariusz testowy **nie zawiera gotowej, prawidłowej odpowiedzi podsuniętej w prompcie** (eliminacja wady laboratoryjnej z opcją 2).
    - Model wykonawcy musi samodzielnie wybrać i wykonać dozwolony krok narzędziowy w oparciu o cel biznesowy, a Obserwator musi poprawnie zakwalifikować jego działanie.
11. **Weryfikacja procedury Rollbacku:**
    - Testowe wycofanie zmian odtwarza pełną, identyczną z pierwotną konfigurację obu zadań crona i ich plików stanu.

---

## 10. Samodzielny przegląd specyfikacji (Spec Self-Review)

Przegląd zgodności z wytycznymi Superpowers (`brainstorming` & `writing-plans`):

1. **Skanowanie braków i placeholderów (Placeholder Scan):**
   - Czy w dokumencie znajdują się frazy typu „TBD”, „TODO”, „do ustalenia później”?
   - *Wynik:* **PASS**. Wszystkie sekcje, identyfikatory, schematy danych i zachowania zostały zdefiniowane. Kwestie podlegające badaniu w planie implementacji zostały jawnie oznaczone jako propozycje inżynieryjne z konkretnymi wartościami domyślnymi.
2. **Spójność wewnętrzna (Internal Consistency):**
   - Czy role i uprawnienia nie kolidują ze sobą?
   - *Wynik:* **PASS**. Zapewniono pełną spójność: Obserwator nie posiada uprawnień wykonawczych ani autonomicznego unblock. Zasada pojedynczego writera obowiązuje dla wiadomości listy oraz kart roboczych. Wyeliminowano ryzyko podwójnego aktywnego zapisu w trakcie wdrożenia.
3. **Ocena zakresu (Scope Check):**
   - Czy specyfikacja nie próbuje zaimplementować kodu w tej karcie?
   - *Wynik:* **PASS**. Dokument pozostaje wyłącznie specyfikacją architektoniczną. Zero modyfikacji plików produkcyjnych, konfiguracji crona, usług czy baz danych.
4. **Jednoznaczność wymagań (Ambiguity Check):**
   - Czy precyzyjnie oddzielono fakty od hipotez i nazwano luki transportowe?
   - *Wynik:* **PASS**. Wyniki badań pilotażowych oznaczono jako `INCONCLUSIVE` z podaniem pełnego mianownika (>=80 completion). Jasno określono brak bezpośredniego IPC pomiędzy cronem a headless workerem, odrzucono plik w worktree jako naruszenie autorytetu i zdefiniowano bezpieczną ścieżkę `undeliverable` + handoff do Otta.

---

## 11. Przekazanie i następne kroki (Handoff)

- Niniejsza specyfikacja techniczna została utrwalona na dedykowanej gałęzi `docs/shared-goal-supervision-spec` w repozytorium `hermes-agent` i zaktualizowana w ramach PR #14.
- Następny krok w cyklu Superpowers: **Przedstawienie poprawionej specyfikacji przez Otta do pisemnego odbioru Oskara**.
- Zgodnie z wytycznymi Superpowers, faza `writing-plans` (tworzenie szczegółowego planu implementacji TDD) zostanie uruchomiona dopiero po uzyskaniu formalnej akceptacji Oskara dla niniejszego dokumentu.
