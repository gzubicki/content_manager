import regex as re
from django import forms
EMOJI_RX = re.compile(r"\p{Emoji}")

def validate_post_text_for_channel(post):
    t = post.text
    ch = post.channel
#     if not t.startswith("⚡️"):
#         raise forms.ValidationError("Lead musi zaczynać się od ⚡️")
#     if ch.no_links_in_text and ("http://" in t or "https://" in t):
#         raise forms.ValidationError("Linki tylko w polach meta, nie w treści")
    if len(t) > ch.max_chars:
        raise forms.ValidationError(f"Za długie (> {ch.max_chars} znaków)")
#     if ch.footer_text not in t:
#         raise forms.ValidationError("Brak stopki kanału")
#     n_emoji = len(EMOJI_RX.findall(t))
#     if n_emoji < ch.emoji_min:
#         raise forms.ValidationError("Dodaj emoji (min)")
#     if n_emoji > ch.emoji_max:
#         raise forms.ValidationError("Za dużo emoji")
