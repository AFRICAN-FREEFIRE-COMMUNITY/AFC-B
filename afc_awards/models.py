from django.db import models
from afc_auth.models import User

# Create your models here.


class Section(models.Model):
    name = models.CharField(max_length=150, unique=True)
    max_votes = models.PositiveIntegerField()  # e.g., 12 for content creators, 13 for esports awards

    def __str__(self):
        return self.name
    

class Category(models.Model):
    category_id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=150, unique=True)
    section = models.ForeignKey(Section, on_delete=models.CASCADE)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Nominee(models.Model):
    nominee_id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=150, unique=True)
    video_url = models.URLField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class CategoryNominee(models.Model):
    category = models.ForeignKey(Category, on_delete=models.CASCADE)
    nominee = models.ForeignKey(Nominee, on_delete=models.CASCADE)

    class Meta:
        unique_together = ("category", "nominee")


class Vote(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    section = models.ForeignKey(Section, on_delete=models.CASCADE, related_name="votes")
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="votes")
    nominee = models.ForeignKey(Nominee, on_delete=models.CASCADE, related_name="votes")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "category")

    def __str__(self):
        return f"{self.user} voted for {self.nominee} in {self.category}"
