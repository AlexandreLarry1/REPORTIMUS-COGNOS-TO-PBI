-- [dim_calendrier]
CREATE OR REPLACE TABLE dim_calendrier AS
SELECT
    Date,
    Annee,
    Trimestre,
    Mois,
    NomMois,
    Semaine,
    JourSemaine,
    NomJour,
    EstWeekend,
    EstJourFerie,
    Semestre,
    AnneeMois,
    AnneeTrimestreTxt
FROM dim_calendrier_src;

-- [dim_clients]
CREATE OR REPLACE TABLE dim_clients AS
SELECT
    ClientID,
    Nom,
    Prenom,
    Email,
    Telephone,
    DateNaissance,
    Segment,
    Pays,
    Ville,
    DateEntreeRelation,
    Conseiller,
    ProfilRisque,
    StatutKYC
FROM dim_clients_src;

-- [dim_conseillers]
CREATE OR REPLACE TABLE dim_conseillers AS
SELECT
    ConseillerID,
    Nom,
    Prenom,
    Email,
    Bureau,
    Equipe,
    AnneeExperience,
    Certification,
    NbClients,
    AuM_EUR
FROM dim_conseillers_src;

-- [dim_instruments]
CREATE OR REPLACE TABLE dim_instruments AS
SELECT
    InstrumentID,
    NomInstrument,
    TypeInstrument,
    "Classe Actif",
    Devise,
    Pays,
    ISIN,
    Emetteur,
    NiveauRisque,
    FraisGestion_pct
FROM dim_instruments_src;

-- [dim_portefeuilles]
CREATE OR REPLACE TABLE dim_portefeuilles AS
SELECT
    PortefeuilleID,
    ClientID,
    TypePortefeuille,
    DateOuverture,
    Devise,
    Statut,
    ObjectifInvestissement,
    HorizonInvestissement,
    BenchmarkRef
FROM dim_portefeuilles_src;

-- [fact_transactions]
CREATE OR REPLACE TABLE fact_transactions AS
SELECT
    TransactionID,
    PortefeuilleID,
    InstrumentID,
    DateTransaction,
    TypeTransaction,
    Quantite,
    PrixUnitaire,
    MontantBrut,
    Frais,
    MontantNet,
    Devise,
    TauxChange_EUR,
    MontantNet_EUR,
    Statut,
    Courtier,
    Operateur
FROM fact_transactions_src
WHERE Statut IN ('Exécuté', 'Execute');

-- [fact_positions]
CREATE OR REPLACE TABLE fact_positions AS
SELECT
    SnapshotDate,
    PortefeuilleID,
    InstrumentID,
    Quantite,
    PrixRevientUnitaire,
    PrixMarcheUnitaire,
    ValeurAchat_EUR,
    ValeurMarche_EUR,
    PnL_EUR,
    PnL_pct,
    PoidsPortefeuille_pct,
    Devise
FROM fact_positions_src;

-- [fact_performances]
CREATE OR REPLACE TABLE fact_performances AS
SELECT
    PeriodeDate,
    PortefeuilleID,
    Rendement_pct,
    Benchmark_pct,
    Alpha_pct,
    Volatilite_pct,
    RatioSharpe,
    MaxDrawdown_pct,
    ValeurPortefeuille_EUR,
    FluxNets_EUR,
    RendementCumule_pct
FROM fact_performances_src;

-- [fact_cours_historiques]
CREATE OR REPLACE TABLE fact_cours_historiques AS
SELECT
    Date,
    InstrumentID,
    PrixOuverture,
    PrixHaut,
    PrixBas,
    PrixCloture,
    Volume,
    Devise
FROM fact_cours_historiques_src;

-- [fact_alertes_risque]
CREATE OR REPLACE TABLE fact_alertes_risque AS
SELECT
    AlerteID,
    PortefeuilleID,
    ClientID,
    DateAlerte,
    TypeAlerte,
    Severite,
    Statut,
    Description,
    ValeurMesure,
    SeuilAlerte,
    DateResolution
FROM fact_alertes_risque_src;

-- [map_macroclasse]
CREATE OR REPLACE TEMP TABLE map_macroclasse AS
SELECT * FROM (
    VALUES
        ('Actions', 'Risque'),
        ('Obligations HY', 'Risque'),
        ('Private Equity', 'Risque'),
        ('Taux', 'Defensif'),
        ('Monétaire', 'Defensif'),
        ('Épargne', 'Defensif'),
        ('Immobilier', 'Diversifie'),
        ('Matières Premières', 'Diversifie'),
        ('Obligations Convertibles', 'Diversifie'),
        ('Monetaire', 'Defensif'),
        ('Epargne', 'Defensif'),
        ('Matieres Premieres', 'Diversifie')
) AS t("Classe Actif", "Macro Classe Actif");

-- [dim_instruments_tmp]
CREATE OR REPLACE TEMP TABLE dim_instruments_tmp AS
SELECT
    *,
    COALESCE(
        (SELECT "Macro Classe Actif"
         FROM map_macroclasse
         WHERE "Classe Actif" = dim_instruments."Classe Actif"),
        'Non classifie'
    ) AS "Macro Classe Actif"
FROM dim_instruments;

DROP TABLE dim_instruments;
CREATE OR REPLACE TABLE dim_instruments AS
SELECT * FROM dim_instruments_tmp;
DROP TABLE dim_instruments_tmp;

-- [map_severiteprio]
CREATE OR REPLACE TEMP TABLE map_severiteprio AS
SELECT * FROM (
    VALUES
        ('Critique', 1),
        ('Haute', 2),
        ('Modérée', 3),
        ('Faible', 4),
        ('Moderee', 3)
) AS t(Severite, "Priorite Numerique");

-- [tempdates]
CREATE OR REPLACE TEMP TABLE tempdates AS
SELECT DISTINCT TRY_STRPTIME(DateTransaction, '%m/%d/%Y') AS TmpDate
FROM fact_transactions
WHERE DateTransaction IS NOT NULL AND DateTransaction != '-'
UNION
SELECT DISTINCT TRY_STRPTIME(SnapshotDate, '%m/%d/%Y') AS TmpDate
FROM fact_positions
WHERE SnapshotDate IS NOT NULL AND SnapshotDate != '-'
UNION
SELECT DISTINCT TRY_STRPTIME(PeriodeDate, '%m/%d/%Y') AS TmpDate
FROM fact_performances
WHERE PeriodeDate IS NOT NULL AND PeriodeDate != '-'
UNION
SELECT DISTINCT TRY_STRPTIME(DateAlerte, '%m/%d/%Y') AS TmpDate
FROM fact_alertes_risque
WHERE DateAlerte IS NOT NULL AND DateAlerte != '-';

-- [mastercalendar]
CREATE OR REPLACE TABLE mastercalendar AS
SELECT
    TmpDate AS Date,
    EXTRACT(YEAR FROM TmpDate) AS Annee,
    EXTRACT(MONTH FROM TmpDate) AS MoisNum,
    STRFTIME(TmpDate, '%m') AS Mois,
    'T' || CEIL(EXTRACT(MONTH FROM TmpDate) / 3.0) AS TrimestreLabel,
    EXTRACT(WEEK FROM TmpDate) AS SemaineISO,
    EXTRACT(DAYOFWEEK FROM TmpDate) AS JourSemaineNum,
    STRFTIME(TmpDate, '%A') AS JourSemaineNom,
    'S' || CASE WHEN EXTRACT(MONTH FROM TmpDate) <= 6 THEN 1 ELSE 2 END AS SemestreLabel,
    CAST(EXTRACT(YEAR FROM TmpDate) AS VARCHAR) || '-' || RIGHT('0' || CAST(EXTRACT(MONTH FROM TmpDate) AS VARCHAR), 2) AS AAAA_MM
FROM tempdates
WHERE TmpDate IS NOT NULL;

DROP TABLE tempdates;

-- [kpi_portefeuilles]
CREATE OR REPLACE TEMP TABLE kpi_portefeuilles AS
SELECT
    PortefeuilleID,
    SUM(CAST(MontantNet_EUR AS DOUBLE)) FILTER (WHERE TypeTransaction = 'Achat') AS "Total Investi (EUR)",
    SUM(CAST(MontantNet_EUR AS DOUBLE)) FILTER (WHERE TypeTransaction = 'Vente') AS "Total Cede (EUR)",
    SUM(CAST(MontantNet_EUR AS DOUBLE)) FILTER (WHERE TypeTransaction IN ('Dividende', 'Coupon')) AS "Total Dividendes (EUR)",
    SUM(CAST(MontantNet_EUR AS DOUBLE)) FILTER (WHERE TypeTransaction = 'Frais') AS "Total Frais (EUR)",
    COUNT(DISTINCT TransactionID) AS "Nb Transactions",
    COUNT(DISTINCT InstrumentID) AS "Nb Instruments Trades",
    MIN(TRY_STRPTIME(DateTransaction, '%m/%d/%Y')) AS "Premiere Transaction",
    MAX(TRY_STRPTIME(DateTransaction, '%m/%d/%Y')) AS "Derniere Transaction"
FROM fact_transactions
WHERE Statut IN ('Exécuté', 'Execute')
GROUP BY PortefeuilleID;

-- [kpi_port_client]
CREATE OR REPLACE TEMP TABLE kpi_port_client AS
SELECT
    kp.PortefeuilleID,
    kp."Total Investi (EUR)" AS "Total Investi (EUR)",
    kp."Total Dividendes (EUR)" AS "Total Dividendes (EUR)",
    dp.ClientID
FROM kpi_portefeuilles kp
LEFT JOIN dim_portefeuilles dp ON kp.PortefeuilleID = dp.PortefeuilleID;

-- [kpi_clients]
CREATE OR REPLACE TABLE kpi_clients AS
SELECT
    ClientID,
    SUM(CAST("Total Investi (EUR)" AS DOUBLE)) AS "Total Investi Client (EUR)",
    SUM(CAST("Total Dividendes (EUR)" AS DOUBLE)) AS "Total Dividendes Client (EUR)",
    COUNT(DISTINCT PortefeuilleID) AS "Nb Portefeuilles"
FROM kpi_port_client
GROUP BY ClientID;

DROP TABLE kpi_port_client;
DROP TABLE kpi_portefeuilles;