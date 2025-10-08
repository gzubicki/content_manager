from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("posts", "0005_alter_channel_id_alter_post_id_alter_postmedia_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="post",
            name="source_metadata",
            field=models.JSONField(blank=True, default=dict, verbose_name="Metadane źródłowe"),
        ),
        migrations.AddField(
            model_name="postmedia",
            name="resolver",
            field=models.CharField(blank=True, default="", max_length=64, verbose_name="Resolver"),
        ),
        migrations.AddField(
            model_name="postmedia",
            name="reference_data",
            field=models.JSONField(blank=True, default=dict, verbose_name="Dane źródła"),
        ),
    ]
