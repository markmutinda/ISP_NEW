from rest_framework import viewsets, status, permissions, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.db.models import Q, Count, F
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend

from ..models import KnowledgeBaseArticle, FAQ
from ..serializers import (
    KnowledgeBaseArticleSerializer, KnowledgeBaseArticleDetailSerializer,
    FAQSerializer, KnowledgeBaseArticleCreateSerializer,
    KnowledgeBaseSearchSerializer
)
from apps.core.permissions import IsAdmin, IsAdminOrStaff


class KnowledgeBaseArticleViewSet(viewsets.ModelViewSet):
    """ViewSet for knowledge base articles"""
    queryset = KnowledgeBaseArticle.objects.filter(status='published')
    serializer_class = KnowledgeBaseArticleSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['category', 'status', 'is_featured', 'is_pinned']
    search_fields = ['title', 'content', 'excerpt', 'tags']
    ordering_fields = ['published_at', 'view_count', 'created_at']
    ordering = ['-published_at', '-created_at']
    
    def get_permissions(self):
        """Set permissions based on action"""
        if self.action in ['list', 'retrieve', 'search', 'categories', 'trending']:
            permission_classes = [AllowAny]
        elif self.action in ['create', 'update', 'partial_update', 'destroy']:
            permission_classes = [IsAuthenticated, IsAdmin | IsAdminOrStaff]
        else:
            permission_classes = [IsAuthenticated]
        return [permission() for permission in permission_classes]
    
    def get_queryset(self):
        """Filter queryset based on user permissions"""
        user = self.request.user
        
        if user.is_authenticated and (user.role in ['admin', 'staff'] or user.is_superuser):
            # Staff can see all articles
            return KnowledgeBaseArticle.objects.all()
        
        # Public users can only see published articles
        return KnowledgeBaseArticle.objects.filter(status='published')
    
    def get_serializer_class(self):
        """Return appropriate serializer class"""
        if self.action == 'retrieve':
            return KnowledgeBaseArticleDetailSerializer
        elif self.action == 'create':
            return KnowledgeBaseArticleCreateSerializer
        return KnowledgeBaseArticleSerializer
    
    def retrieve(self, request, *args, **kwargs):
        """Retrieve article and increment view count"""
        instance = self.get_object()
        instance.view_count = F('view_count') + 1
        instance.last_viewed_at = timezone.now()
        instance.save(update_fields=['view_count', 'last_viewed_at'])
        instance.refresh_from_db()
        
        serializer = self.get_serializer(instance)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def categories(self, request):
        """Get list of article categories with counts"""
        categories = KnowledgeBaseArticle.objects.filter(status='published').values(
            'category'
        ).annotate(
            count=Count('id'),
            last_updated=Max('updated_at')
        ).order_by('category')
        
        # Format category names
        category_choices = dict(KnowledgeBaseArticle.CATEGORIES)
        formatted_categories = []
        for cat in categories:
            formatted_categories.append({
                'value': cat['category'],
                'label': category_choices.get(cat['category'], cat['category']),
                'count': cat['count'],
                'last_updated': cat['last_updated']
            })
        
        return Response(formatted_categories)
    
    @action(detail=True, methods=['post'])
    def mark_helpful(self, request, pk=None):
        """Mark article as helpful or not helpful"""
        article = self.get_object()
        helpful = request.data.get('helpful', None)
        
        if helpful is None:
            return Response(
                {'error': 'helpful parameter is required (true/false)'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if helpful:
            article.helpful_yes = F('helpful_yes') + 1
        else:
            article.helpful_no = F('helpful_no') + 1
        
        article.save(update_fields=['helpful_yes', 'helpful_no'])
        article.refresh_from_db()
        
        return Response({
            'status': 'success',
            'message': 'Feedback recorded',
            'helpful_yes': article.helpful_yes,
            'helpful_no': article.helpful_no,
            'helpful_percentage': article.helpful_percentage
        })


class FAQViewSet(viewsets.ModelViewSet):
    """ViewSet for FAQs"""
    queryset = FAQ.objects.filter(is_active=True)
    serializer_class = FAQSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['category', 'is_featured']
    search_fields = ['question', 'answer', 'tags']
    ordering_fields = ['display_order', 'view_count']
    ordering = ['display_order', 'category']
    
    def get_permissions(self):
        """Set permissions based on action"""
        if self.action in ['list', 'retrieve', 'categories']:
            permission_classes = [AllowAny]
        else:
            permission_classes = [IsAuthenticated, IsAdmin | IsAdminOrStaff]
        return [permission() for permission in permission_classes]
    
    def retrieve(self, request, *args, **kwargs):
        """Retrieve FAQ and increment view count"""
        instance = self.get_object()
        instance.view_count = F('view_count') + 1
        instance.save(update_fields=['view_count'])
        instance.refresh_from_db()
        
        serializer = self.get_serializer(instance)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def mark_helpful(self, request, pk=None):
        """Mark FAQ as helpful or not helpful"""
        faq = self.get_object()
        helpful = request.data.get('helpful', None)
        
        if helpful is None:
            return Response(
                {'error': 'helpful parameter is required (true/false)'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if helpful:
            faq.helpful_yes = F('helpful_yes') + 1
        else:
            faq.helpful_no = F('helpful_no') + 1
        
        faq.save(update_fields=['helpful_yes', 'helpful_no'])
        faq.refresh_from_db()
        
        return Response({
            'status': 'success',
            'message': 'Feedback recorded',
            'helpful_yes': faq.helpful_yes,
            'helpful_no': faq.helpful_no,
            'helpful_percentage': faq.helpful_percentage
        })