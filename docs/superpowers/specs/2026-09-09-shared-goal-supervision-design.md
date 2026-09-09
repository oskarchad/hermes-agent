# Specyfikacja: Wspólny Nadzór Celu i Lista Discord „Teraz robimy” (10 min)

Data: 2026-09-09
Autor: Wrench · Engineer
Odbiorca: Oskar Chądzyński (przez Otta / Captain)
Status: DRAFT — do odbioru pisemnego specu przez Oskara i Otta (bez wdrożenia)
Lokalizacja dokumentu: `docs/superpowers/specs/2026-09-09-shared-goal-supervision-design.md`
Związane wątki: Discord thread `1546608648438943794`, wiadomość Oskara `1547268730487312485`

---

## 1. Streszczenie wykonawcze i cel nadrzędny (North-Star)

### 1.1. Cel biznesowy
Zbudować spójny, oszczędny i odporny na awarie mechanizm nadzoru pracy agentów na poziomie celu operatora (Oskara), łączący dwa procesy:
1. **Listę Discord „Teraz robimy”** (aktualizowaną co 10 minut w channel `1546944099725344819`, message `1546944153152258070`), która ma wiernie odzwierciedlać rzeczywisty stan tematów i blokad bez marnowania wywołań LLM.
2. **Krótką, niezależną sesję Obserwatora Celu**, która aktywnie pomaga wykonawcom powrócić do celu biznesowego, ocenia zgodność działań z briefem Otta i wytycznymi Oskara, wykrywa dryf zakresu i fałszywe zatrzymania — bez powtarzania tego samego odczytu i bez naruszania architektury Kanbanu.

### 1.2. Problem do rozwiązania
- Obecna lista Discord co 10 min uruchamia pełny cykl bez precyzyjnego mechanizmu wykrywania, czy w wątkach pojawiły się materialne zmiany, co generuje zbędne wywołania modeli przy braku nowości.
- Obserwacja zadań i wykrywanie zagubienia agentów były dotąd rozproszone pomiędzy pasywny skrypt monitorujący `captain_watch_slim.py` (Captain Watch), który raportuje wyłącznie wąskie sygnały bazodanowe do Otta, a doraźne analizy post-factum.
- Badanie pilotażowe (zanalizowane w `t_1fe15805` i skorygowane w `t_f4e89ee9/scorecard.md`) wykazało, że sam prompt/skill wstrzykiwany do wykonawcy nie gwarantuje powrotu do celu i nie rozwiązuje problemu utknięcia w pętli narzędziowej. Wykonawca w pętli nie jest obiektywnym sędzią własnego stanu.
- Jednocześnie wykonywanie osobnego, pełnego scrapowania i analizy LLM przez dwa niezależne procesy (listę zadań i obserwatora) powielałoby zużycie tokenów, koszty API i ryzyko desynchronizacji interpretacji.

### 1.3. Kluczowa decyzja architektoniczna
Zastosowanie **wspólnej warstwy przyrostowego odczytu i detekcji zmian (Shared Collector / Sensor)** oraz **wspólnego semantycznego pakietu zdarzeń**. Jeśli brak nowych faktów: `0 wywołań LLM` i `0 mutacji / PATCH`. Jeśli pojawiły się nowe fakty: jedna, zwięzła analiza semantyczna może zasilić zaktualizowaną treść listy oraz sformułować konkretną wskazówkę korygującą (steer/handoff) dla wykonawcy lub Otta.

---

## 2. Granice, role i decyzje operatora vs propozycje techniczne

### 2.1. Niezmienne decyzje operatora (Operator Gates & Boundaries)
Poniższe punkty zostały zatwierdzone przez Oskara i Otta; specyfikacja traktuje je jako nienaruszalne aksjomaty:
1. **Brak autonomicznych uprawnień wykonawczych Obserwatora:** Obserwator celu NIGDY nie wykonuje autonomicznego `unblock`, `merge`, `push to main`, `deploy`, `restart`, `kill` procesu ani zmiany `goal` zadania.
2. **Nienaruszalność cyklu życia Kanbanu:** Kanban pozostaje jedynym źródłem prawdy dla stanu zadań. Nie tworzymy nowej bazy zadań, równoległego schedulera ani drugiego systemu kolejkowego.
3. **Pojedynczy writer dla każdego zasobu:**
   - Wiadomość listy zadań na Discordzie (`1546944153152258070`) ma dokładnie jednego writera (job listy).
   - Karta Kanbanowa i jej worktree mają dokładnie jednego aktywnego writera (wyznaczony worker profilu roboczego, np. Wrench).
   - Wznowienie zadania zatrzymanego/zablokowanego należy wyłącznie do Otta (jako Captaina).
4. **Bramki Oskara (Human-in-the-Loop):** Decyzje dotyczące wydatków, produkcji, zmian celów biznesowych, wdrożeń oraz publikacji wymagają jawnej zgody Oskara. Obserwator ani Otto nie mogą ich obejść etykietą automatyczną.
5. **Zachowanie formatu i kanału listy:** Docelowa lista pozostaje w kanale `lista-zadan` (guild `1546331229240954923`, channel `1546944099725344819`, message `1546944153152258070`). Pięć sekcji: **W trakcie**, **Zablokowane**, **Do akceptacji**, **Czeka / do domknięcia**, **Ostatnio zrobione**. Treść pochodzi wyłącznie z rozmów Discorda, nie z surowego importu bazy Kanbanu.
6. **Rozdzielenie dokumentacji od wdrożenia:** Niniejsza karta (`t_2bb6f616`) jest wyłącznie specyfikacją (dokumentem). Nie wdraża kodu w systemie produkcyjnym.

### 2.2. Propozycje inżynieryjne (podlegające weryfikacji w planie implementacji)
1. **Architektura procesu:** Konsolidacja harmonogramu w jeden połączony cykl 10-minutowy z dwuetapowym przetwarzaniem: faza detekcji mechanicznej (skrypt Python / pre-script crona) -> faza semantyczna LLM (uruchamiana tylko przy `delta > 0`).
2. **Kanał doręczenia korekty do aktywnego workera:** Wykorzystanie mechanizmu bezpiecznego wstrzykiwania informacji (`steer`) po zakończeniu wywołania narzędzia, bez unieważniania prompt cache i bez podszywania się pod rolę użytkownika.
3. **Progi czasowe braku postępu:** Propozycja bezpiecznych, wstępnych progów detekcji bezczynności (np. 2 kolejne ticki monitora = 20 min bez narzędzia lub eventu) z uwzględnieniem legalnych długich operacji.

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
- **Werdykt badania Counta:** Status pilota: `INCONCLUSIVE`. Mikrotest 60 wywołań w izolacji z syntetycznym promptem nie odzwierciedlał warunków floty.
- **Brak transportu w pętli:** Wstrzykiwanie reguł do promptu wykonawcy nie zapobiega zapętleniu w narzędziach.
- **Konieczność zewnętrznego arbitra:** Obserwator musi mieć dostęp do literalnego celu Oskara (z cytatem i ID wiadomości Discorda) oraz briefu Otta, aby móc skrytykować nawet nadmierny lub zniekształcony brief przekazany wykonawcy.
- **Odrzucenie autonomicznego unblock:** Obserwator nie może samowolnie odblokowywać kart (`analysis.md:177` zawierało błędną propozycję, którą nadrzędny raport `scorecard.md` bezwzględnie odrzucił).

---

## 4. Odrzucone alternatywy architektoniczne

### Alternatywa 1: Całkowicie niezależny, stale działający agent-obserwator (Continuous LLM Watcher)
- **Opis:** Uruchomienie dedykowanego demona lub osobnego joba crona, który co 2-5 minut odpytuje LLM o stan każdego aktywnego zadania i wątku Discorda.
- **Dlaczego odrzucono:**
  - *Marnotrawstwo tokenów i kosztów:* W 80-90% przypadków zadanie wykonuje normalny, długi krok obliczeniowy lub wątek nie zawiera nowych wiadomości. Pytanie LLM co kilka minut generuje ogromny koszt bez żadnego zysku poznawczego.
  - *Zanikanie kontekstu i halucynacje:* Ciągłe odpytywanie prowadzi do szumu w alertach (alert fatigue), zwłaszcza gdy agent myli normalny brak commita z awarią.
  - *Brak synchronizacji z listą:* Istniałyby dwa niezależne byty czytające te same wątki Discorda, co prowadziłoby do rozbieżnych interpretacji stanu zadań na liście i w alertach.

### Alternatywa 2: Wyłącznie pasywny skill dla wykonawcy (Self-Supervision Skill Only)
- **Opis:** Pozostawienie nadzoru samemu wykonawcy poprzez dodanie mu instrukcji „sprawdzaj swój cel przed każdym wywołaniem narzędzia” (podejście testowane w `t_1fe15805`).
- **Dlaczego odrzucono:**
  - *Efekt pętli poznawczej:* Agent, który wszedł w błędną ścieżkę (np. walka z brakującą zależnością, próba refaktoryzacji biblioteki zamiast obejścia problemu), interpretuje swoje działania jako „absolutnie konieczne dla celu”.
  - *Brak obiektywizmu:* Samodzielna weryfikacja w tym samym oknie kontekstowym podlega zmęczeniu kontekstu i ograniczeniom pamięci podręcznej.
  - *Brak ochrony przed zniekształconym briefem:* Jeśli Kapitan (Otto) przekazał zbyt szeroki brief, wykonawca nie ma zewnętrznego punktu odniesienia, by go zakwestionować.

### Dlaczego wybrano podejście hybrydowe (Wspólna detekcja mechaniczna + celowana analiza semantyczna)
- Mechaniczny skrypt-sensor (zero tokenów LLM) sprawdza kursory, sumy kontrolne i nowe wiadomości/zdarzenia.
- Jeśli brak materialnych zmian: zero wywołań modeli, brak zapisu.
- Jeśli nastąpiła zmiana: jedna krótka sesja analityczna przetwarza nowe fakty, aktualizuje listę „Teraz robimy” i — jeśli wystąpiła anomalia — generuje precyzyjną notatkę korygującą dla wykonawcy lub Otta.

---

## 5. Architektura systemu i przepływ danych

### 5.1. Schemat blokowy przepływu (ASCII Diagram)

```text
+-----------------------------------------------------------------------------------+
|                            WARSTWA ŹRÓDEŁ DANYCH                                  |
|                                                                                   |
|   +-------------------------------+       +-----------------------------------+   |
|   | Discord API: Wątki i Rozmowy |       | Kanban SQLite: Tasks / Events     |   |
|   | (Prawda o celach i decyzjach) |       | (Prawda o procesach i narzędziach)|   |
|   +---------------+---------------+       +-----------------+-----------------+   |
+-------------------|-----------------------------------------|---------------------+
                    |                                         |
                    v                                         v
+-----------------------------------------------------------------------------------+
|               WARSTWA 1: SENSOR MECHANICZNY (Zero LLM / Zero Spend)               |
|                                                                                   |
|   1. Odczyt kursorów Discorda (ostatnie ID per wątek/kanał).                      |
|   2. Odczyt zdarzeń Kanbanu od ostatniego przetworzonego `event_id`.              |
|   3. Detekcja materialnych zmian:                                                 |
|      - Nowa wiadomość decyzyjna Oskara / raport Otta?                             |
|      - Nowy stan zadania (running -> blocked / failed)?                           |
|      - Brak aktywności narzędziowej wykonawcy > T_idle?                           |
|                                                                                   |
|         [Czy wykryto materialną zmianę w którymkolwiek źródle?]                   |
|                   /                                   \                           |
|             (NIE) /                                     \ (TAK)                   |
|                  v                                       v                        |
|       +----------------------+              +-------------------------+           |
|       |     NO-OP (CISZA)    |              | Utwórz pakiet zdarzeń   |           |
|       | - 0 wywołań LLM      |              | (Snapshot + Deltas)     |           |
|       | - Brak PATCH Discorda|              +------------+------------+           |
|       | - Zapisz kursory     |                           |                        |
|       +----------------------+                           |                        |
+----------------------------------------------------------|------------------------+
                                                           v
+-----------------------------------------------------------------------------------+
|               WARSTWA 2: ANALIZA SEMANTYCZNA (Pojedynczy cykl LLM)                |
|                                                                                   |
|   Input: Skondensowany pakiet delty (nowe wiadomości + stan zadań + cele Oskara)  |
|                                                                                   |
|   Zadanie A: Aktualizacja listy Discord        Zadanie B: Ocena dryfu celu         |
|   - Klasyfikacja tematów:                     - Czy worker realizuje brief?       |
|     W trakcie / Zablokowane /                  - Czy pojawił się fałszywy stop?    |
|     Do akceptacji / Czeka / Zrobione          - Czy potrzebna jest korekta?       |
|                                                                                   |
|   Output A: Nowy tekst listy                  Output B: Werdykt nadzoru           |
+-------------------|--------------------------------------|------------------------+
                    |                                      |
                    v                                      v
+------------------------------------+   +------------------------------------------+
|       WARSTWA 3A: PUBLIKACJA       |   |       WARSTWA 3B: DORĘCZENIE KOREKTY     |
|                                    |   |                                          |
|  PATCH exact target message        |   |  Przypadek 1: Worker RUNNING             |
|  channel: 1546944099725344819      |   |  -> Dostarczenie `steer` po narzędziu     |
|  message: 1546944153152258070      |   |     (brak mutacji prompt cache)          |
|  (Weryfikacja content_before/CAS)  |   |                                          |
|                                    |   |  Przypadek 2: Worker BLOCKED / STOPPED   |
|                                    |   |  -> Handoff do sesji Otta                |
|                                    |   |     (Otto decyduje o wznowieniu)         |
+------------------------------------+   +------------------------------------------+
```

---

## 6. Szczegółowe kontrakty komponentów

### 6.1. Kontrakt Sensora i Kursora (Incremental Read Model)
- **Stan sensora** przechowywany w profilu Otta (`/home/hermes/.hermes/profiles/otto/cron/goal-supervision-state.json`):
  ```json
  {
    "schema_version": 1,
    "last_check_timestamp": 1788968000,
    "discord_cursors": {
      "channel_id": "latest_read_message_id"
    },
    "kanban_cursors": {
      "last_event_id": 78950,
      "tracked_tasks": {
        "t_xxxx": {
          "run_id": 5231,
          "last_tool_timestamp": 1788967800,
          "last_heartbeat_timestamp": 1788967900,
          "idle_strike_count": 0,
          "last_steer_hash": "a1b2c3..."
        }
      }
    },
    "published_list_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
  }
  ```
- **Zasada nieprzeskakiwania kursorów (No Cursor Skipping):** Kursor przesuwa się wyłącznie do pozycji potwierdzonych jako przeczytane. W przypadku błędu API (np. timeout Discorda na jednym kanale), stan tego kanału pozostaje niezmieniony, a bieg jest klasyfikowany jako `partial_read`. Nie wolno zgłaszać `unchanged`, gdy część źródeł zgłosiła błąd.
- **Odporność na restarty i awarie:** Sensor zapisuje stan kursorów transakcyjnie (zapis do pliku tymczasowego + `os.replace`). Przy restarcie procesu czytanie zaczyna się od ostatniego bezpiecznego stanu.

### 6.2. Reguły Wykrywania Zdarzeń i Triggery Obserwatora
Obserwator nie alarmuje przy każdym ticku. Sygnał korygujący jest generowany **wyłącznie** przy spełnieniu jednego z poniższych warunków:
1. **Nowa blokada (Blocker Trigger):** Zadanie przeszło w stan `blocked` lub `failed`, albo w Discordzie pojawił się meldunek o niemożności wykonania kroku.
2. **Fałszywy stop (False-Stop Trigger):** Wykonawca zablokował zadanie z powodu braku nieistotnego elementu (np. brak opcjonalnego pliku, odmowa wykonania zakazanego polecenia zamiast użycia dozwolonego zamiennika), podczas gdy główny cel może być kontynuowany.
3. **Dryf zakresu (Scope Drift Trigger):** Wykonawca rozpoczął modyfikację plików lub instalację narzędzi niezwiązanych z celem nadrzędnym (np. próba naprawy całego frameworka, pisanie kodu zamiast tworzenia dokumentu).
4. **Pętla bezowocna (Spin Loop Trigger):** Wykonawca wykonał co najmniej 3 kolejne powtórzenia tego samego polecenia narzędziowego z identycznym błędem lub brakiem rezultatu merytorycznego.
5. **Przekroczenie okna postępu (Idle Timeout Trigger):** Zadanie w stanie `running` przez ponad 20 minut nie wywołało żadnego narzędzia, nie złożyło commita ani nie zaraportowało postępu, a jednocześnie nie jest to udokumentowany, legalny długi proces (np. kompilacja/trening oznaczony przez `kanban_heartbeat(note=...)`).

**Co NIE jest triggerem alarmu:**
- Prawidłowy `kanban_heartbeat` z opisem postępu długiej operacji.
- Oczekiwanie na normalne zakończenie procesu w tle (`healthy wait`).
- Sam upływ 10 minut ticka przy braku nowych zdarzeń.
- Własny raport workera złożony na karcie, jeśli raportuje normalny postęp.

### 6.3. Analiza Semantyczna i Format Notatki Korygującej (Steer & Handoff)
Gdy sensor wykryje anomalię, uruchamiana jest krótka sesja arbitra (LLM). Prompt arbitra zawiera:
1. **Prawdziwy cel Oskara:** Dosłowny cytat polecenia Oskara z Discorda wraz z ID wiadomości.
2. **Brief Otta:** Polecenie przekazane wykonawcy w karcie.
3. **Obserwowane fakty:** Lista ostatnich wywołań narzędzi, komend gita, błędów i edytowanych plików.

Arbiter generuje ustrukturyzowaną notatkę korygującą według ścisłego schematu:
```yaml
korekta_celu:
  task_id: "t_xxxx"
  run_id: 5231
  odbiorca: "wrench" # lub "otto", jeśli worker jest zatrzymany
  status_celu: "DRIFT | FALSE_STOP | SPIN_LOOP | STALLED"
  dowod_fakt: "Worker od 15 minut próbuje zainstalować pakiet X, który nie jest wymagany w briefie."
  korekta_kroku: "Pomiń pakiet X. Wykorzystaj wbudowane narzędzie Y i skup się wyłącznie na dokumencie Z."
  dozwolone_nastepne_dzialanie: "Zapisz plik docs/... i uruchom self-review."
  bramka_operatora: false # true tylko jeśli wymagana jest decyzja Oskara
```

### 6.4. Zamknięcie Pętli Nadzoru (Closed-Loop Enforcement)
Zgodnie z zasadą: `received != accepted != executed != useful progress`:
1. **Dostarczenie (`received`):** Potwierdzenie doręczenia komunikatu korygującego (zapis w zdarzeniach zadania lub potwierdzenie mechanizmu steer).
2. **Akceptacja (`accepted`):** Wykonawca w kolejnym kroku musi odnieść się do korekty (w komentarzu `METHOD DELTA` lub logu działania) — albo ją przyjmując, albo odrzucając z twardym dowodem technicznym.
3. **Wykonanie (`executed`):** Wykonanie właściwego kroku (np. zmiana edytowanego pliku, przejście do dozwolonej komendy).
4. **Postęp (`useful progress`):** Kolejny tick sensora sprawdza, czy stan anomali ustąpił. Jeśli po doręczeniu korekty wykonawca nadal tkwi w pętli przez kolejny tick:
   - Następuje eskalacja do Otta (jako Captaina).
   - Otto podejmuje decyzję o zatrzymaniu/reclaim zadania przez oficjalny cykl życia Kanbanu (`kanban_block` lub restart z nowym briefem).
   - Żadne rutynowe korekty techniczne nie są wypychane bezpośrednio do Oskara. Do Oskara trafiają wyłącznie prawdziwe bramki decyzyjne (`OPERATOR GATE`).

---

## 7. Transport i Integracje: Istniejące vs Brakujące

Kluczowy element analizy inżynieryjnej — oddzielenie tego, co system potrafi dzisiaj, od koniecznych uzupełnień:

### 7.1. Co istnieje w kodzie (Production Truth)
1. **CLI / Gateway `/steer` (`gateway/run_busy.py:452-480`, `run_agent.py:825`):**
   - W środowisku interaktywnym (gateway, CLI) istnieje w pełni działająca metoda `running_agent.steer(text)`.
   - Tekst wstrzykiwany przez `steer` jest bezpiecznie doklejany do wyników ostatniego wywołania narzędzia (`_apply_pending_steer_to_tool_results`).
   - Zachowuje **nienaruszalność prompt cache** i ścisłą naprzemienność ról (nie tworzy syntetycznej wiadomości użytkownika).
2. **Mechanizm monitorów w cronie (`cron/jobs.py`, `cron/scheduler.py`):**
   - Obsługa `monitor_script`, który przed uruchomieniem joba sprawdza warunki i przy braku zmian (`[SILENT]` lub ten sam hash) pomija wywołanie LLM.
3. **Kanban API i baza zdarzeń (`hermes_cli/kanban_*.py`):**
   - Pełna historia zdarzeń w `kanban.db` (`spawned`, `heartbeat`, `blocked`, `comment`, `completed`).

### 7.2. Brakujące elementy transportu (Missing Gaps)
1. **Brak bezpośredniego transportu IPC z procesu Crona do headless Workera Kanbanu:**
   - Worker Kanbanu jest uruchamiany jako niezależny proces w tle (`hermes_cli/kanban_db_dispatch.py:2836` przez `subprocess.Popen` w `systemd_scope` lub `process_session`).
   - Proces joba crona (uruchamiany w profilu `otto`) NIE ma referencji do obiektu pamięciowego `AIAgent` działającego w osobnym procesie workera (`wrench`).
   - Wywołanie `/steer` przez CLI dotyczy wyłącznie sesji gatewaya/TUI, a nie oddzielnego procesu batchowego workera.
2. **Minimalne uzupełnienie architektoniczne (Propozycja do planu):**
   - **Najprostszy bezpieczny mechanizm:** Worker przed wykonaniem kolejnego narzędzia sprawdza obecność pliku sterującego w swoim katalogu roboczym (np. `.steer_inbox.json`) lub odczytuje nowe komentarze ze swojej karty o typie `steer` / `supervision`.
   - **Dopóki taka ścieżka nie zostanie zaimplementowana w kodzie:** Korekty dla pracującego workera muszą być przekazywane przez oficjalny komentarz na karcie Kanbanowej (`kanban_comment`), a w przypadku zablokowania lub braku reakcji — przez bezpośredni handoff do Otta (`deliver: bot-chat:otto`), który jako jedyny dysponuje uprawnieniem do wznowienia karty.

---

## 8. Rollout, Rollback i Migracja Jobów

### 8.1. Stan obecny przed zmianą
- Działają dwa oddzielne joby w profilu `otto`:
  - `147ed6e7ed22` (Discord lista co 10 min, bez monitora, ciągłe wywołania LLM).
  - `6af4395e7e47` (Captain Watch co 5 min, z monitorem `captain_watch_slim.py`).

### 8.2. Plan wdrożenia (Staged Cutover)
1. **Faza 0 (Bieżąca):** Spisanie i zaakceptowanie niniejszej specyfikacji. Żaden job nie jest wyłączany ani modyfikowany.
2. **Faza 1 (Przygotowanie skryptu sensora w worktree):**
   - Opracowanie zunifikowanego skryptu sensora `shared_goal_detector.py`, który w jednym przebiegu sprawdza zarówno kursory Discorda, jak i stan bazy Kanbanu.
   - Weryfikacja działania w trybie dry-run (bez wysyłania PATCH i bez alertów).
3. **Faza 2 (Połączenie w jeden cronjob z jawną bramką):**
   - Modyfikacja joba `147ed6e7ed22`: podpięcie skryptu detektora jako `monitor_script`.
   - Ustawienie domyślnego taktu na 10 minut.
   - Jeśli monitor wykryje brak zmian: zwraca `[SILENT]`, co zapobiega uruchomieniu agenta i edycji Discorda (0 LLM, 0 PATCH).
   - Jeśli monitor wykryje zmiany: uruchamia zoptymalizowany prompt aktualizujący listę oraz badający cel.
4. **Faza 3 (Wygaszenie duplikatu):**
   - Po potwierdzeniu stabilnego działania nowego potoku przez 24 godziny: wstrzymanie (`hermes cron pause 6af4395e7e47`).

### 8.3. Plan wycofania (Rollback)
W razie jakichkolwiek problemów z poprawnością listy lub fałszywych alarmów:
1. Usunięcie `monitor_script` z konfiguracji joba `147ed6e7ed22` przywraca w 100% poprzednie, w pełni niezależne działanie listy co 10 minut.
2. Wznowienie `6af4395e7e47` przywraca dotychczasowy Captain Watch Slim.
3. Czas wykonania procedury rollbacku: poniżej 2 minut, bez utraty danych i bez wpływu na aktywne karty robocze.

---

## 9. Kryteria akceptacji (Acceptance Criteria dla przyszłych testów)

Gdy specyfikacja zostanie zaakceptowana i przejdzie do etapu planowania oraz implementacji, rozwiązanie musi spełniać poniższe, weryfikowalne kryteria:

1. **Test braku zmian (Unchanged NO-OP):**
   - Brak nowych wiadomości w wątkach Discorda i brak zmian w tabeli `events` Kanbanu skutkuje dokładnie: `0 wywołań LLM`, `0 żądań PATCH do Discord API`, brak nowych wpisów w logach błędów.
2. **Idempotentność i odporność na restarty:**
   - Pojedyncze nowe zdarzenie (np. jeden komentarz w Discordzie) zostaje ocenione dokładnie raz, nawet w przypadku restartu procesu demona crona pomiędzy tickami.
3. **Reakcja na nowe fakty (New Evidence Triggers Eval):**
   - Dodanie nowej wiadomości przez Oskara lub zmiana stanu zadania natychmiast wyzwala ewaluację w najbliższym ticku sensora.
4. **Ochrona przed zgubieniem deadline'u:**
   - Brak nowych wiadomości w Discordzie NIE powoduje uśpienia licznika bezczynności (idle timer). Przekroczenie progu T_idle generuje sygnał nadzorczy niezależnie od ciszy w czacie.
5. **Izolacja nieaktualnych porad (Stale Run Guard):**
   - Porada wygenerowana dla `run_id: N` nie może zostać omyłkowo doręczona do nowo zrespawnowanego `run_id: N+1`.
6. **Weryfikacja pętli zwrotnej:**
   - System rejestruje faktyczny stan doręczenia i reakcji (`received`, `accepted`, `executed`, `useful progress`), a w przypadku braku reakcji generuje pojedynczy, celowany handoff do Otta.
7. **Brak fałszywych alarmów na legalnych operacjach:**
   - Zadanie zgłaszające regularne `kanban_heartbeat` z opisem długiej kompilacji lub testu nie jest oznaczane jako zawieszone.
8. **Prawidłowa obsługa błędów częściowych (Partial Read Safety):**
   - Wystąpienie błędu sieciowego (np. 500 z Discord API) nie przesuwa kursora dla tego kanału i nie jest klasyfikowane jako „brak zmian”.
9. **Transparentne mierzenie kosztów:**
   - Telemetria rozróżnia zapytania pominięte (koszt 0), zapytania z pamięci podręcznej (cache hit) oraz pełne wywołania generatywne.

---

## 10. Samodzielny przegląd specyfikacji (Spec Self-Review)

Przegląd zgodności z wytycznymi Superpowers (`brainstorming` & `writing-plans`):

1. **Skanowanie braków i placeholderów (Placeholder Scan):**
   - Czy w dokumencie znajdują się frazy typu „TBD”, „TODO”, „do ustalenia później”?
   - *Wynik:* BRAK. Wszystkie sekcje, kontrakty, identyfikatory (guild, channel, message, job ID), schematy danych i zachowania zostały w pełni zdefiniowane.
2. **Spójność wewnętrzna (Internal Consistency):**
   - Czy role nie kolidują ze sobą?
   - *Wynik:* Pełna spójność. Zachowano zasadę jednego writera: Obserwator nie ma uprawnień do edycji bazy zadań ani publikacji na Discordzie poza zdefiniowanym potokiem. Otto pozostaje jedynym Captainem wznawiającym zadania. Oskar zachowuje wszystkie bramki właścicielskie.
3. **Ocena zakresu (Scope Check):**
   - Czy specyfikacja nie próbuje zaimplementować kodu w tej karcie?
   - *Wynik:* Zachowano ścisłą dyscyplinę. Dokument opisuje wyłącznie architekturę, kontrakty i punkty styku. Zero modyfikacji plików produkcyjnych, zero zmian w bazach danych i usługach systemowych.
4. **Jednoznaczność wymagań (Ambiguity Check):**
   - Czy zidentyfikowano ograniczenia transportowe?
   - *Wynik:* Tak. Jawnie nazwano różnicę między istniejącym `steer` w gateway/CLI a brakiem takiego IPC w headless workerach Kanbanu, wskazując bezpieczną ścieżkę komentarza i handoffu do czasu wdrożenia bezpośredniego kanału.

---

## 11. Przekazanie i następne kroki (Handoff)

- Dokument specyfikacji został zapisany i utrwalony w repozytorium na gałęzi `docs/shared-goal-supervision-spec`.
- Następny krok w cyklu Superpowers: **Przegląd specyfikacji przez Oskara za pośrednictwem Otta**.
- Dopiero po uzyskaniu formalnej akceptacji Oskara i Otta dla niniejszego dokumentu, uruchomiony zostanie etap `writing-plans` w celu przygotowania szczegółowego planu implementacji ze scenariuszami testowymi TDD.
