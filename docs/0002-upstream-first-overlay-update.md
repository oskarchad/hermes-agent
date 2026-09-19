# ADR 0002: Upstream-First Overlay Update Pattern for Major Refactors

Status: Accepted

## Context

Podczas aktualizacji Hermes Agent z dużą liczbą zmian w upstreamie (np. PR #102117 wprowadzający -34% LOC, rozbicie monolitów / god-files i dekompozycję struktury modułów), tradycyjne podejście oparte na śledzeniu każdego commita w historii gita lub próba mechanicznego `git rebase` / `git merge` prowadzi do:
1. Gigantycznego narzutu analitycznego (tysiące commitów upstreamu, które są już przetestowane i stabilne).
2. Wyczerpywania budżetu pętli agentów (limity iteracji przy analizowaniu tysięcy konfliktów w rozbitych plikach).
3. Ryzyka błędnego odtworzenia starej struktury plików zamiast przyjęcia nowej, odchudzonej architektury upstreamu.

## Decision

Przyjmujemy strategię **Upstream-First Overlay Update**:

1. **Upstream jako Czysty Fundament (Clean Slate Foundation):**
   - Nie analizujemy pojedynczych commitów ani nie rozwiązujemy konfliktów historycznych krok po kroku w gicie.
   - Nowa gałąź robocza startuje bezpośrednio z czystego, najnowszego commita upstream (`origin/main`).
   - Całość kodu upstreamu traktujemy jako sprawdzoną i autorytatywną.

2. **Dekompozycja i Mapowanie Naszej Delty (Feature-by-Feature Porting):**
   - Nasz custom code (delte funkcjonalną) traktujemy jako zbiór odrębnych, modułowych dodatków (np. Captain Inbox, Kanban review lifecycle & process isolation, headless MCP OAuth).
   - Nie przenosimy starych plików hurtowo. Sprawdzamy nową strukturę katalogów w upstreamie i implementujemy/dopinamy nasze ficzery we właściwych, nowych miejscach architektonicznych.
   - Jeśli upstream rozwiązał dany problem natywnie — porzucamy nasz custom patch bez przenoszenia.

3. **Weryfikacja Oparta o Kontrakt (Contract-First Verification):**
   - Po wpięciu danego dodatku uruchamiamy dedykowany zestaw testów tylko dla niego.
   - Zapewnia to hermetyczność, eliminuje długie pętle rebase'a i gwarantuje, że korzystamy z najnowszych optymalizacji kodu upstreamu.

## Consequences

- Przebudowa środowiska jest szybka, deterministyczna i nie blokuje się w pętlach gita.
- Nasz kod natychmiast korzysta ze zrefaktoryzowanej, modularnej architektury upstreamu.
- Kod produkcyjny na serwerze pozostaje nienaruszony aż do pełnego zweryfikowania i zatwierdzenia przez Gauge i Oskara.
