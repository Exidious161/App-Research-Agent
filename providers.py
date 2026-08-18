"""The research agent's model backends.

Every backend does the same job: take a system prompt and a question, search the
live web, and hand back the reply text plus every URL the search tool actually
returned. That second list is what makes the evidence check possible -- it lets
agent.py test a cited URL against pages the tool really fetched, instead of
taking the model's word for it.

Add a backend by implementing research() and registering it in REGISTRY.
"""

import os


class ProviderError(RuntimeError):
    pass


def _is_url(value):
    return isinstance(value, str) and value.startswith("http")


# --------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------

class AnthropicProvider:
    """Claude with the server-side web_search tool.

    Search runs on Anthropic's side, so there is no browser to drive and no
    scraper to maintain. Results come back inline as web_search_tool_result
    blocks, which is exactly the citation list the evidence check needs.
    """

    name = "anthropic"
    DEFAULT_MODEL = "claude-opus-5"

    # Dynamic-filtering search, on Opus 4.6+ / Sonnet 4.6+. Older models only
    # have the basic tool, so a rejected tool type falls back instead of dying.
    SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 8}
    SEARCH_TOOL_BASIC = {"type": "web_search_20250305", "name": "web_search", "max_uses": 8}

    MAX_PAUSE_RESUMES = 4

    def __init__(self, model=None, api_key=None):
        try:
            import anthropic
        except ImportError:
            raise ProviderError("anthropic SDK missing. pip install anthropic")
        key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise ProviderError("No ANTHROPIC_API_KEY")
        self.model = model or self.DEFAULT_MODEL
        self.client = anthropic.Anthropic(api_key=key)
        self._search_tool = self.SEARCH_TOOL

    def _create(self, system, messages):
        return self.client.beta.messages.create(
            model=self.model,
            max_tokens=8000,
            system=system,
            messages=messages,
            tools=[self._search_tool],
            # Route around a safety refusal rather than losing the app entirely.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )

    def research(self, system, prompt):
        messages = [{"role": "user", "content": prompt}]
        urls, text_parts = [], []

        for _ in range(self.MAX_PAUSE_RESUMES):
            try:
                response = self._create(system, messages)
            except Exception as error:
                # An older model rejects the dated tool type. Drop to the basic
                # tool once and retry, rather than failing the whole run.
                if "web_search_20260209" in str(error) \
                        and self._search_tool is self.SEARCH_TOOL:
                    self._search_tool = self.SEARCH_TOOL_BASIC
                    response = self._create(system, messages)
                else:
                    raise

            if getattr(response, "stop_reason", None) == "refusal":
                detail = getattr(response, "stop_details", None)
                raise ProviderError("refused (%s)" % getattr(detail, "category", "?"))

            urls.extend(self._urls(response))
            text_parts.extend(self._text(response))

            # A long search turn can stop early and expects to be handed back.
            if getattr(response, "stop_reason", None) != "pause_turn":
                break
            messages.append({"role": "assistant", "content": response.content})

        return "\n".join(text_parts).strip(), urls

    @staticmethod
    def _text(response):
        return [block.text for block in response.content
                if getattr(block, "type", None) == "text"
                and getattr(block, "text", None)]

    @staticmethod
    def _urls(response):
        found = []
        for block in response.content:
            if getattr(block, "type", None) != "web_search_tool_result":
                continue
            results = getattr(block, "content", None)
            # Success gives a list of results; a failure gives one error object.
            if not isinstance(results, list):
                continue
            for result in results:
                url = getattr(result, "url", None)
                if _is_url(url):
                    found.append(url)
        return found

    def available_models(self):
        try:
            return [m.id for m in self.client.models.list()]
        except Exception:
            return []


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------

class GeminiProvider:
    """Gemini with Google Search grounding. The original backend."""

    name = "gemini"
    DEFAULT_MODEL = "gemini-3.7-flash"
    # Grounded-search models get renamed and retired often enough that pinning
    # one name is how the run dies three months later.
    FALLBACKS = ["gemini-3.7-flash", "gemini-3-flash", "gemini-2.5-flash",
                 "gemini-2.0-flash"]

    def __init__(self, model=None, api_key=None):
        try:
            from google import genai
        except ImportError:
            raise ProviderError("google-genai missing. pip install google-genai")
        key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not key:
            raise ProviderError("No GEMINI_API_KEY")
        self.client = genai.Client(api_key=key)
        self.model = self._resolve(model or self.DEFAULT_MODEL)

    def _resolve(self, preferred):
        available = self.available_models()
        if not available:
            return preferred
        for name in [preferred] + [m for m in self.FALLBACKS if m != preferred]:
            if name in available:
                return name
        raise ProviderError("None of %s are available to this key. It can see: %s"
                            % ([preferred] + self.FALLBACKS,
                               ", ".join(available[:15]) or "nothing"))

    def available_models(self):
        try:
            return [m.name.removeprefix("models/") for m in self.client.models.list()]
        except Exception:
            return []

    def research(self, system, prompt):
        interaction = self.client.interactions.create(
            model=self.model,
            system_instruction=system,
            input=prompt,
            tools=[{"type": "google_search"}],
            # Headroom over the ~300 tokens of JSON, because thinking tokens
            # count against this too and a truncated reply parses as no JSON.
            generation_config={"max_output_tokens": 4000},
        )
        return (interaction.output_text or "").strip(), self._urls(interaction)

    @staticmethod
    def _urls(interaction):
        found = []
        for step in getattr(interaction, "steps", None) or []:
            if getattr(step, "type", None) != "model_output":
                continue
            for block in getattr(step, "content", None) or []:
                for note in getattr(block, "annotations", None) or []:
                    if getattr(note, "type", None) == "url_citation" \
                            and _is_url(getattr(note, "url", None)):
                        found.append(note.url)
        return found


# --------------------------------------------------------------------------

REGISTRY = {"anthropic": AnthropicProvider, "gemini": GeminiProvider}

# Checked in this order when --provider is not given.
DETECT = [("anthropic", "ANTHROPIC_API_KEY"),
          ("gemini", "GEMINI_API_KEY"),
          ("gemini", "GOOGLE_API_KEY")]


def detect():
    for name, env_var in DETECT:
        if os.getenv(env_var):
            return name
    return None


def build(provider=None, model=None):
    provider = provider or detect()
    if not provider:
        raise ProviderError(
            "No API key found. Copy .env.example to .env and set one of "
            "ANTHROPIC_API_KEY or GEMINI_API_KEY.")
    if provider not in REGISTRY:
        raise ProviderError("Unknown provider %r. Known: %s"
                            % (provider, ", ".join(REGISTRY)))
    return REGISTRY[provider](model=model)
