from fastapi import FastAPI, Depends, HTTPException, Query, Request
import requests as http
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from sqlalchemy import create_engine, Column, Integer, Float, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from pydantic import BaseModel, Field
from datetime import datetime, timedelta, timezone
import math
import os

def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
from typing import List, Optional
from dotenv import load_dotenv

load_dotenv()

# --- CONFIGURATION BASE DE DONNÉES (PostgreSQL) ---
SQLALCHEMY_DATABASE_URL = os.environ["DATABASE_URL"]
GOOGLE_MAPS_API_KEY = os.environ["GOOGLE_MAPS_API_KEY"]
engine = create_engine(SQLALCHEMY_DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- MODÈLES ---
class Evaluation(Base):
    __tablename__ = "evaluations"
    id = Column(Integer, primary_key=True, index=True)
    latitude = Column(Float, index=True)
    longitude = Column(Float, index=True)
    crowd_level = Column(Integer, nullable=True) # Échelle 1-5
    noise_level = Column(Integer, nullable=True) # Échelle 1-5
    timestamp = Column(DateTime, default=utcnow)

Base.metadata.create_all(bind=engine)

# Pydantic pour la validation
class EvalCreate(BaseModel):
    latitude: float
    longitude: float
    crowd_level: Optional[int] = Field(None, ge=1, le=5)
    noise_level: Optional[int] = Field(None, ge=1, le=5)

class PointAggrege(BaseModel):
    latitude: float
    longitude: float
    avg_crowd: Optional[float]
    avg_noise: Optional[float]
    weight: float

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="Sensitive API")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

RATE_LIMIT_SECONDS = 10 * 60
GRID_CELL = 0.0005  # Half a 111m grid cell width

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- FONCTION DE PONDÉRATION ---
def calculate_weight(eval_time: datetime, eval_lat: float, eval_lon: float, center_lat: float, center_lon: float):
    # 1. Pondération par le temps (Demi-vie de 2 heures par exemple)
    age_hours = (utcnow() - eval_time).total_seconds() / 3600
    time_weight = math.exp(-0.346 * age_hours) # Réduit le poids à 50% après 2h
    
    # 2. Pondération par la distance (Formule de Haversine simplifiée pour le calcul)
    R = 6371 # Rayon de la terre en km
    dlat = math.radians(eval_lat - center_lat)
    dlon = math.radians(eval_lon - center_lon)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(center_lat)) * math.cos(math.radians(eval_lat)) * math.sin(dlon/2)**2
    distance_km = 2 * R * math.asin(math.sqrt(a))
    
    # Si c'est à plus de 500m, on ignore ou on met un poids très faible
    if distance_km > 0.5:
        return 0
    distance_weight = math.exp(-5 * distance_km) 
    
    return time_weight * distance_weight

# --- ROUTES ---
@app.post("/evaluations/")
@limiter.limit("10/minute")
def add_evaluation(request: Request, eval: EvalCreate, db: Session = Depends(get_db)):
    time_limit = utcnow() - timedelta(seconds=RATE_LIMIT_SECONDS)
    grid_lat = round(eval.latitude, 3)
    grid_lon = round(eval.longitude, 3)
    recent = db.query(Evaluation).filter(
        Evaluation.timestamp >= time_limit,
        Evaluation.latitude >= grid_lat - GRID_CELL,
        Evaluation.latitude <  grid_lat + GRID_CELL,
        Evaluation.longitude >= grid_lon - GRID_CELL,
        Evaluation.longitude <  grid_lon + GRID_CELL,
    ).first()
    if recent:
        remaining = RATE_LIMIT_SECONDS - (utcnow() - recent.timestamp).total_seconds()
        mins, secs = int(remaining // 60), int(remaining % 60)
        raise HTTPException(status_code=429, detail=f"Ce lieu a déjà été noté récemment. Réessayez dans {mins}m{secs:02d}s.")

    db_eval = Evaluation(
        latitude=eval.latitude,
        longitude=eval.longitude,
        noise_level=eval.noise_level,
        crowd_level=eval.crowd_level,
    )
    db.add(db_eval)
    db.commit()
    db.refresh(db_eval)
    return {"status": "success", "id": db_eval.id}

@app.get("/map-data/", response_model=List[PointAggrege])
@limiter.limit("60/minute")
def get_map_data(request: Request, lat: float, lon: float, radius_km: float = Query(2.0, gt=0, le=200), db: Session = Depends(get_db)):
    time_limit = utcnow() - timedelta(hours=12)
    lat_delta = radius_km / 111
    lon_delta = radius_km / (111 * math.cos(math.radians(lat)))
    evals = db.query(Evaluation).filter(
        Evaluation.timestamp >= time_limit,
        Evaluation.latitude.between(lat - lat_delta, lat + lat_delta),
        Evaluation.longitude.between(lon - lon_delta, lon + lon_delta),
    ).all()
    
    # Simplification pour le MVP : on regroupe par zones (ex: grille de 100m)
    # Dans un système de prod, on utiliserait PostGIS pour faire un clustering spatial côté BDD.
    clusters = {}
    
    for e in evals:
        # Arrondir lat/lon pour créer une grille (environ 100m de précision)
        grid_lat = round(e.latitude, 3)
        grid_lon = round(e.longitude, 3)
        key = (grid_lat, grid_lon)
        
        weight = calculate_weight(e.timestamp, e.latitude, e.longitude, grid_lat, grid_lon)
        if weight <= 0.01: continue
        
        if key not in clusters:
            clusters[key] = {"crowd_sum": 0, "crowd_weight": 0, "noise_sum": 0, "noise_weight": 0}
            
        if e.crowd_level:
            clusters[key]["crowd_sum"] += e.crowd_level * weight
            clusters[key]["crowd_weight"] += weight
        if e.noise_level:
            clusters[key]["noise_sum"] += e.noise_level * weight
            clusters[key]["noise_weight"] += weight

    # Formatage de la réponse
    results = []
    for (glat, glon), data in clusters.items():
        avg_crowd = data["crowd_sum"] / data["crowd_weight"] if data["crowd_weight"] > 0 else None
        avg_noise = data["noise_sum"] / data["noise_weight"] if data["noise_weight"] > 0 else None
        
        if avg_crowd or avg_noise:
            results.append(PointAggrege(
                latitude=glat, 
                longitude=glon, 
                avg_crowd=avg_crowd, 
                avg_noise=avg_noise,
                weight=max(data["crowd_weight"], data["noise_weight"])
            ))
            
    return results

@app.get("/places/autocomplete/")
@limiter.limit("30/minute")
def places_autocomplete(request: Request, input: str):
    resp = http.get(
        "https://maps.googleapis.com/maps/api/place/autocomplete/json",
        params={"input": input, "key": GOOGLE_MAPS_API_KEY, "language": "fr"},
        timeout=5,
    )
    return resp.json()

@app.get("/places/details/")
@limiter.limit("30/minute")
def places_details(request: Request, place_id: str):
    resp = http.get(
        "https://maps.googleapis.com/maps/api/place/details/json",
        params={"place_id": place_id, "fields": "geometry", "key": GOOGLE_MAPS_API_KEY},
        timeout=5,
    )
    return resp.json()