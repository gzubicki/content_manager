from django import forms

def validate_post_text_for_channel(post):
    t = post.text
    ch = post.channel
    if len(t) > ch.max_chars:
        raise forms.ValidationError(f"Za długie (> {ch.max_chars} znaków)")
