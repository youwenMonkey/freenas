from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('services', '0021_add_ups_hostsync_field'),
    ]

    operations = [
        migrations.AddField(
            model_name='cifs',
            name='cifs_srv_min_protocol',
            field=models.CharField(choices=[('NT1', 'NT1'), ('SMB2_02', 'SMB2_02'), ('SMB3_00', 'SMB3_00')], default='SMB2_02', help_text='The minimum protocol version that will be supported by the server', max_length=120, verbose_name='Server minimum protocol'),
        ),
    ]
