from django.db import models

# Create your models here.


class Product(models.Model):
    STATUS_CHOICES = [
        ('in_stock', 'In Stock'),
        ('out_of_stock', 'Out of Stock'),
    ]

    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=255)
    description = models.TextField()
    diamonds = models.PositiveIntegerField(default=0)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    image = models.ImageField(upload_to='products/')
    stock = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='in_stock')

    def save(self, *args, **kwargs):
        if self.stock == 0:
            self.status = 'out_of_stock'
        else:
            self.status = 'in_stock'
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name
