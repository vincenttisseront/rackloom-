"""Launcher : seul composant autorisé à piloter le moteur de conteneurs.

Le worker lui transmet une spécification fermée (type de job, espace de travail, stack) ;
le launcher choisit lui-même l'image (liste blanche par empreinte), les montages,
le réseau et les limites associés à ce type de job. Aucun champ de la spécification
ne permet de choisir une image, une commande, un montage ou un réseau arbitraire.
"""
