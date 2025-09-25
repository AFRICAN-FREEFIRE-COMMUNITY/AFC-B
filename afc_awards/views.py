from django.shortcuts import render
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from afc_auth.models import AdminHistory, User
from afc_awards.models import Category, CategoryNominee, Nominee, Section, Vote
# Create your views here.

@api_view(['POST'])
def add_new_category(request):
    if request.method == 'POST':
        # --- Authenticate user ---
        session_token = request.headers.get("Authorization")
        if not session_token or not session_token.startswith("Bearer "):
            return Response({"error": "Invalid or missing Authorization header"}, status=status.HTTP_400_BAD_REQUEST)

        session_token = session_token.split(" ")[1]
        try:
            user = User.objects.get(session_token=session_token)
        except User.DoesNotExist:
            return Response({"error": "Invalid session token"}, status=status.HTTP_401_UNAUTHORIZED)

        name = request.data.get('name')
        section_id = request.data.get('section_id')
    
        if not name or not section_id:
            return Response({"error": "Name and section_id are required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            section = Section.objects.get(id=section_id)
        except Section.DoesNotExist:
            return Response({"error": "Section not found"}, status=status.HTTP_404_NOT_FOUND)

        category = Category.objects.create(name=name, section=section)

        AdminHistory.objects.create(
            admin_user=user,
            action="added_category",
            description=f"Added new category '{name}' (ID: {category.category_id}) in section '{section.name}' (ID: {section.id})"
        )
        return Response({"id": category.category_id, "name": category.name, "section": category.section.name}, status=status.HTTP_201_CREATED)


@api_view(['GET'])
def view_all_categories(request):
    categories = Category.objects.all()
    data = [{"id": category.category_id, "name": category.name, "section": category.section.name} for category in categories]
    return Response(data, status=status.HTTP_200_OK)



@api_view(['DELETE'])
def delete_category(request):
    # --- Authenticate user ---
    session_token = request.headers.get("Authorization")
    if not session_token or not session_token.startswith("Bearer "):
        return Response({"error": "Invalid or missing Authorization header"}, status=status.HTTP_400_BAD_REQUEST)

    session_token = session_token.split(" ")[1]
    try:
        user = User.objects.get(session_token=session_token)
    except User.DoesNotExist:
        return Response({"error": "Invalid session token"}, status=status.HTTP_401_UNAUTHORIZED)

    try:
        category_id = request.data.get('category_id')
        category = Category.objects.get(category_id=category_id)
        category.delete()

        AdminHistory.objects.create(
            admin_user=user,
            action="deleted_category",
            description=f"Deleted category '{category.name}' (ID: {category_id})"
        )
        return Response(status=status.HTTP_204_NO_CONTENT)
    except Category.DoesNotExist:
        return Response({"error": "Category not found"}, status=status.HTTP_404_NOT_FOUND)


@api_view(['POST'])
def add_new_nominee(request):
    if request.method == 'POST':
        # --- Authenticate user ---
        session_token = request.headers.get("Authorization")
        if not session_token or not session_token.startswith("Bearer "):
            return Response({"error": "Invalid or missing Authorization header"}, status=status.HTTP_400_BAD_REQUEST)

        session_token = session_token.split(" ")[1]
        try:
            user = User.objects.get(session_token=session_token)
        except User.DoesNotExist:
            return Response({"error": "Invalid session token"}, status=status.HTTP_401_UNAUTHORIZED)

        name = request.data.get('name')
        video_url = request.data.get('video_url')

        if not name:
            return Response({"error": "Name is required"}, status=status.HTTP_400_BAD_REQUEST)

        nominee = Nominee.objects.create(name=name, video_url=video_url)

        AdminHistory.objects.create(
            admin_user=user,
            action="added_nominee",
            description=f"Added nominee '{nominee.name}' (ID: {nominee.nominee_id})"
        )
        return Response({"id": nominee.nominee_id, "name": nominee.name, "video_url": nominee.video_url}, status=status.HTTP_201_CREATED)
    

@api_view(['GET'])
def view_all_nominees(request):
    nominees = Nominee.objects.all()
    data = [{"id": nominee.nominee_id, "name": nominee.name} for nominee in nominees]
    return Response(data, status=status.HTTP_200_OK)


@api_view(['DELETE'])
def delete_nominee(request):
    # --- Authenticate user ---
    session_token = request.headers.get("Authorization")
    if not session_token or not session_token.startswith("Bearer "):
        return Response({"error": "Invalid or missing Authorization header"}, status=status.HTTP_400_BAD_REQUEST)

    session_token = session_token.split(" ")[1]
    try:
        user = User.objects.get(session_token=session_token)
    except User.DoesNotExist:
        return Response({"error": "Invalid session token"}, status=status.HTTP_401_UNAUTHORIZED)

    try:
        nominee_id = request.data.get('nominee_id')
        nominee = Nominee.objects.get(nominee_id=nominee_id)
        nominee.delete()
        AdminHistory.objects.create(
            admin_user=user,
            action="removed_nominee",
            description=f"Removed nominee '{nominee.name}' (ID: {nominee_id}) from category '{category.name}' (ID: {category_id})"
        )
        return Response(status=status.HTTP_204_NO_CONTENT)
    except Nominee.DoesNotExist:
        return Response({"error": "Nominee not found"}, status=status.HTTP_404_NOT_FOUND)
    

@api_view(['POST'])
def add_nominee_to_category(request):
    if request.method == 'POST':
        # --- Authenticate user ---
        session_token = request.headers.get("Authorization")
        if not session_token or not session_token.startswith("Bearer "):
            return Response({"error": "Invalid or missing Authorization header"}, status=status.HTTP_400_BAD_REQUEST)

        session_token = session_token.split(" ")[1]
        try:
            user = User.objects.get(session_token=session_token)
        except User.DoesNotExist:
            return Response({"error": "Invalid session token"}, status=status.HTTP_401_UNAUTHORIZED)

        category_id = request.data.get('category_id')
        nominee_id = request.data.get('nominee_id')

        if not category_id or not nominee_id:
            return Response({"error": "Category ID and Nominee ID are required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            category = Category.objects.get(category_id=category_id)
            nominee = Nominee.objects.get(nominee_id=nominee_id)
            CategoryNominee.objects.create(category=category, nominee=nominee)

            AdminHistory.objects.create(
                admin_user=user,
                action="added_nominee",
                description=f"Added nominee '{nominee.name}' (ID: {nominee_id}) to category '{category.name}' (ID: {category_id})"
            )
            return Response({"message": "Nominee added to category successfully"}, status=status.HTTP_201_CREATED)
        except (Category.DoesNotExist, Nominee.DoesNotExist):
            return Response({"error": "Category or Nominee not found"}, status=status.HTTP_404_NOT_FOUND)


@api_view(['POST'])
def view_nominee_in_category(request):
    try:
        category_id = request.data.get('category_id')
        category = Category.objects.get(category_id=category_id)
        nominees = category.categorynominee_set.all()

        data = [{"id": nominee.nominee.nominee_id, "name": nominee.nominee.name} for nominee in nominees]
        return Response(data, status=status.HTTP_200_OK)
    except Category.DoesNotExist:
        return Response({"error": "Category not found"}, status=status.HTTP_404_NOT_FOUND)
    

@api_view(['DELETE'])
def remove_nominee_from_category(request):
    if request.method == 'DELETE':
        # --- Authenticate user ---
        session_token = request.headers.get("Authorization")
        if not session_token or not session_token.startswith("Bearer "):
            return Response({"error": "Invalid or missing Authorization header"}, status=status.HTTP_400_BAD_REQUEST)

        session_token = session_token.split(" ")[1]
        try:
            user = User.objects.get(session_token=session_token)
        except User.DoesNotExist:
            return Response({"error": "Invalid session token"}, status=status.HTTP_401_UNAUTHORIZED)

        category_id = request.data.get('category_id')
        nominee_id = request.data.get('nominee_id')

        if not category_id or not nominee_id:
            return Response({"error": "Category ID and Nominee ID are required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            category = Category.objects.get(category_id=category_id)
            nominee = Nominee.objects.get(nominee_id=nominee_id)
            category_nominee = CategoryNominee.objects.get(category=category, nominee=nominee)
            category_nominee.delete()

            AdminHistory.objects.create(
                admin_user=user,
                action="removed_nominee",
                description=f"Removed nominee '{nominee.name}' (ID: {nominee_id}) from category '{category.name}' (ID: {category_id})"
            )

            return Response({"message": "Nominee removed from category successfully"}, status=status.HTTP_204_NO_CONTENT)
        except (Category.DoesNotExist, Nominee.DoesNotExist, CategoryNominee.DoesNotExist):
            return Response({"error": "Category or Nominee not found"}, status=status.HTTP_404_NOT_FOUND)
        

@api_view(['POST'])
def add_section(request):
    if request.method == 'POST':
        # --- Authenticate user ---
        session_token = request.headers.get("Authorization")
        if not session_token or not session_token.startswith("Bearer "):
            return Response({"error": "Invalid or missing Authorization header"}, status=status.HTTP_400_BAD_REQUEST)

        session_token = session_token.split(" ")[1]
        try:
            user = User.objects.get(session_token=session_token)
        except User.DoesNotExist:
            return Response({"error": "Invalid session token"}, status=status.HTTP_401_UNAUTHORIZED)

        name = request.data.get('name')
        max_votes = request.data.get('max_votes')

        if not name or not max_votes:
            return Response({"error": "Name and max_votes are required"}, status=status.HTTP_400_BAD_REQUEST)

        section = Section.objects.create(name=name, max_votes=max_votes)

        AdminHistory.objects.create(
            admin_user=user,
            action="added_section",
            description=f"Added new section '{name}' (ID: {section.id}) with max votes {max_votes}"
        )

        return Response({"id": section.id, "name": section.name, "max_votes": section.max_votes}, status=status.HTTP_201_CREATED)


@api_view(['POST'])
def submit_votes(request):
    # --- Authenticate user ---
    session_token = request.headers.get("Authorization")
    if not session_token or not session_token.startswith("Bearer "):
        return Response({"error": "Invalid or missing Authorization header"}, status=status.HTTP_400_BAD_REQUEST)

    session_token = session_token.split(" ")[1]
    try:
        user = User.objects.get(session_token=session_token)
    except User.DoesNotExist:
        return Response({"error": "Invalid session token"}, status=status.HTTP_401_UNAUTHORIZED)

    # --- Extract data ---
    section_id = request.data.get("section_id")
    votes_data = request.data.get("votes", [])  # list of {category_id, nominee_id}

    if not section_id or not votes_data:
        return Response({"error": "Section ID and votes are required"}, status=status.HTTP_400_BAD_REQUEST)

    try:
        section = Section.objects.get(id=section_id)
    except Section.DoesNotExist:
        return Response({"error": "Section not found"}, status=status.HTTP_404_NOT_FOUND)

    # --- Check if already voted in this section ---
    if Vote.objects.filter(user=user, section=section).exists():
        return Response({"error": "You have already voted in this section"}, status=status.HTTP_403_FORBIDDEN)

    # --- Validate number of votes ---
    if len(votes_data) != section.max_votes:
        return Response(
            {"error": f"You must submit exactly {section.max_votes} votes for {section.name}"},
            status=status.HTTP_400_BAD_REQUEST
        )

    # --- Save votes in one transaction ---
    from django.db import transaction
    try:
        with transaction.atomic():
            for v in votes_data:
                category_id = v.get("category_id")
                nominee_id = v.get("nominee_id")
                if not category_id or not nominee_id:
                    raise ValueError("Each vote must include category_id and nominee_id")

                category = Category.objects.get(id=category_id, section=section)
                nominee = Nominee.objects.get(id=nominee_id)

                Vote.objects.create(
                    user=user,
                    section=section,
                    category=category,
                    nominee=nominee
                )

        return Response({"message": "Votes submitted successfully"}, status=status.HTTP_201_CREATED)

    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET'])
def view_all_nominee_in_each_category(request):
    sections = Section.objects.all()
    data = []

    for section in sections:
        categories = section.category_set.all()
        category_list = []

        for category in categories:
            nominees = category.categorynominee_set.all()
            nominee_list = [
                {
                    "id": nominee.nominee.nominee_id,
                    "name": nominee.nominee.name,
                    "video_url": nominee.nominee.video_url
                }
                for nominee in nominees
            ]

            category_list.append({
                "category_id": category.category_id,
                "category_name": category.name,
                "nominees": nominee_list
            })

        data.append({
            "section_id": section.id,
            "section_name": section.name,
            "categories": category_list
        })

    return Response(data, status=status.HTTP_200_OK)


@api_view(['GET'])
def list_sections(request):
    sections = Section.objects.all()
    data = [{"id": section.id, "name": section.name} for section in sections]
    return Response(data, status=status.HTTP_200_OK)


@api_view(['POST'])
def get_section(request):
    section_id = request.data.get("section_id")
    if not section_id:
        return Response({"error": "Section ID is required"}, status=status.HTTP_400_BAD_REQUEST)

    try:
        section = Section.objects.get(id=section_id)
        data = {
            "id": section.id,
            "name": section.name
        }
        return Response(data, status=status.HTTP_200_OK)
    except Section.DoesNotExist:
        return Response({"error": "Section not found"}, status=status.HTTP_404_NOT_FOUND)


@api_view(['GET'])
def get_total_votes(request):
    total_votes = Vote.objects.count()
    return Response({"total_votes": total_votes}, status=status.HTTP_200_OK)